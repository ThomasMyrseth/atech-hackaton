/**
 * @file microphone.cpp
 * @brief I2S MEMS Microphone Module Implementation (ICS-43434)
 *
 * Audio capture runs in a background FreeRTOS task. The task continuously
 * reads I2S data and computes RMS/peak levels for the main loop to query.
 * Supports recording up to 10 seconds of audio into a heap buffer.
 */

#include "microphone.h"
#include <Arduino.h>
#include <driver/i2s.h>
#include <math.h>
#include <string.h>

// Static trampoline for FreeRTOS task
static void captureTaskTrampoline(void* param) {
    ((Microphone*)param)->_captureTaskLoop();
}

Microphone::Microphone(int lrPin, int wsPin, int sckPin, int sdPin)
    : _lrPin(lrPin)
    , _wsPin(wsPin)
    , _sckPin(sckPin)
    , _sdPin(sdPin)
    , _initialized(false)
    , _rmsLevel(0)
    , _peakLevel(0)
    , _sampleCount(0)
    , _recordBuffer(nullptr)
    , _recordLength(0)
    , _recording(false)
    , _audioCallback(nullptr)
    , _audioCallbackCtx(nullptr)
    , _captureTask(nullptr)
{
    memset(_sampleBuffer, 0, sizeof(_sampleBuffer));
}

void Microphone::begin() {
    // Set L/R channel select pin (LOW = left channel)
    pinMode(_lrPin, OUTPUT);
    digitalWrite(_lrPin, LOW);

    i2s_config_t i2s_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate = SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,  // ICS-43434 outputs 24-bit in 32-bit frame
        .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = 0,
        .dma_buf_count = 4,
        .dma_buf_len = 256,
        .use_apll = false,
        .tx_desc_auto_clear = false,
    };

    i2s_pin_config_t pin_config = {
        .bck_io_num = _sckPin,
        .ws_io_num = _wsPin,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num = _sdPin,
    };

    // Use I2S_NUM_1 to avoid conflict with speaker on I2S_NUM_0
    esp_err_t err = i2s_driver_install(I2S_NUM_1, &i2s_config, 0, NULL);
    if (err != ESP_OK) {
        Serial.print("Microphone I2S install failed: ");
        Serial.println(esp_err_to_name(err));
        return;
    }

    err = i2s_set_pin(I2S_NUM_1, &pin_config);
    if (err != ESP_OK) {
        Serial.print("Microphone I2S pin config failed: ");
        Serial.println(esp_err_to_name(err));
        return;
    }

    _initialized = true;

    // Create background capture task on Core 0
    xTaskCreatePinnedToCore(
        captureTaskTrampoline,
        "MicTask",
        8192,
        this,
        2,
        &_captureTask,
        0  // Core 0
    );
}

int16_t Microphone::getSoundLevel() {
    return _rmsLevel;
}

bool Microphone::isSoundDetected(int16_t threshold) {
    return _rmsLevel > threshold;
}

int16_t Microphone::getPeakAmplitude() {
    int16_t peak = _peakLevel;
    _peakLevel = 0;  // Reset after read
    return peak;
}

int Microphone::getSamples(int16_t* dest, int numSamples) {
    int count = min(numSamples, (int)_sampleCount);
    count = min(count, (int)BUFFER_SAMPLES);
    memcpy(dest, _sampleBuffer, count * sizeof(int16_t));
    return count;
}

void Microphone::startRecording() {
    // Free any previous recording
    if (_recordBuffer) {
        free(_recordBuffer);
        _recordBuffer = nullptr;
    }

    // Allocate heap buffer for recording (16-bit mono, 3 seconds = 96KB)
    Serial.printf("Free heap: %d bytes, need: %d bytes\n", ESP.getFreeHeap(), MAX_RECORD_SAMPLES * (int)sizeof(int16_t));
    _recordBuffer = (int16_t*)malloc(MAX_RECORD_SAMPLES * sizeof(int16_t));
    if (!_recordBuffer) {
        Serial.println("Microphone: Failed to allocate recording buffer");
        return;
    }

    _recordLength = 0;
    _recording = true;
    Serial.println("Recording started...");
}

void Microphone::stopRecording() {
    _recording = false;
    Serial.print("Recording stopped. Samples: ");
    Serial.print(_recordLength);
    Serial.print(" (");
    Serial.print((float)_recordLength / SAMPLE_RATE, 1);
    Serial.println("s)");
}

bool Microphone::isRecording() {
    return _recording;
}

const int16_t* Microphone::getRecording() {
    return _recordBuffer;
}

int Microphone::getRecordingLength() {
    return _recordLength;
}

void Microphone::setChannel(bool right) {
    digitalWrite(_lrPin, right ? HIGH : LOW);
}

void Microphone::onAudio(AudioCallback cb, void* ctx) {
    // Set ctx before cb: the capture task checks cb, then reads ctx.
    _audioCallbackCtx = ctx;
    _audioCallback = cb;
}

// ========== Background capture task ==========

void Microphone::_captureTaskLoop() {
    // Read stereo frames: each frame is [Left_32bit, Right_32bit]
    int32_t rawBuffer[BUFFER_SAMPLES * 2];  // Stereo pairs

    while (true) {
        size_t bytesRead = 0;
        esp_err_t err = i2s_read(I2S_NUM_1, rawBuffer, sizeof(rawBuffer), &bytesRead, pdMS_TO_TICKS(100));

        if (err != ESP_OK || bytesRead == 0) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        int totalSamples = bytesRead / sizeof(int32_t);  // L+R interleaved
        int monoSamples = totalSamples / 2;
        // nattbord: velg automatisk kanalen som faktisk har signal (ESP32-S3 kan bytte L/R)
        int64_t sL = 0, sR = 0;
        for (int i = 0; i < monoSamples; i++) {
            int32_t l = rawBuffer[i * 2] >> 16, r = rawBuffer[i * 2 + 1] >> 16;
            sL += (int64_t)l * l; sR += (int64_t)r * r;
        }
        dbgRmsL = (int32_t)sqrt((double)sL / max(1, monoSamples));
        dbgRmsR = (int32_t)sqrt((double)sR / max(1, monoSamples));
        if (activeChannel == 0 && sR > 4 * sL + 1000) activeChannel = 1;
        else if (activeChannel == 1 && sL > 4 * sR + 1000) activeChannel = 0;
        int ch = activeChannel;
        int64_t sumSquares = 0;
        int16_t peak = 0;

        for (int i = 0; i < monoSamples; i++) {
            int32_t v = rawBuffer[i * 2 + ch] >> 13;   // nattbord: 8x forsterkning (var >> 16)
            if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
            int16_t sample = (int16_t)v;
            _sampleBuffer[i] = sample;

            int32_t abs_sample = abs(sample);
            sumSquares += (int64_t)sample * sample;
            if (abs_sample > peak) peak = (int16_t)abs_sample;

            if (_recording && _recordLength < MAX_RECORD_SAMPLES) {
                _recordBuffer[_recordLength++] = sample;
            } else if (_recording && _recordLength >= MAX_RECORD_SAMPLES) {
                _recording = false;
                Serial.println("Recording buffer full, auto-stopped.");
            }
        }

        _sampleCount = monoSamples;
        _rmsLevel = (int16_t)sqrt((double)sumSquares / monoSamples);

        if (peak > _peakLevel) _peakLevel = peak;

        // Streaming tap — gap-free feed for consumers like VoiceAssistant
        AudioCallback cb = _audioCallback;
        if (cb) cb(_sampleBuffer, monoSamples, _audioCallbackCtx);
    }
}
