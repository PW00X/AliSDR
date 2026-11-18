// SPDX-License-Identifier: GPL-3.0-or-later
// C ABI between FalconSDR's Python receiver and dab-cmdline's Mode I decoder.
#include "dab-api.h"
#include <atomic>
#include <map>

using Station = void (*)(uint32_t, const char *, int);
using Audio = void (*)(const int16_t *, int, int);
using Reception = void (*)(bool, int16_t, int32_t);
using Metadata = void (*)(uint32_t, const char *);
using Image = void (*)(uint32_t, const uint8_t *, int);

struct Receiver final : deviceHandler {
    RingBuffer<std::complex<float>> iq{1 << 20};
    API_struct api{};
    void *decoder = nullptr;
    std::map<uint32_t, std::string> names;
    std::map<uint32_t, int> advertised;
    std::atomic<uint32_t> requested{0};
    std::atomic<uint32_t> selected{0};
    std::atomic<Metadata> metadata{nullptr};
    std::atomic<Image> image{nullptr};
    std::string ensemble;
    Station station;
    Audio audio;
    Reception reception;

    Receiver(Station s, Audio a, Reception r) : station(s), audio(a), reception(r) {
        api.dabMode = 1;
        api.thresholdValue = 3;
        api.syncsignal_Handler = [](bool, void *) {};
        api.name_of_ensemble = [](const std::string &name, int32_t, void *context) {
            static_cast<Receiver *>(context)->ensemble = name;
        };
        api.serviceName = [](const std::string &name, int32_t id, uint16_t, void *context) {
            static_cast<Receiver *>(context)->names[static_cast<uint32_t>(id)] = name;
        };
        api.systemdata_Handler = [](bool synced, int16_t snr, int32_t offset, void *context) {
            auto &self = *static_cast<Receiver *>(context);
            self.reception(synced, snr, offset);
            if (synced)
                self.update_services();
        };
        api.dataOut_Handler = [](const char *label, void *context) {
            auto &self = *static_cast<Receiver *>(context);
            const auto sid = self.selected.load();
            if (auto callback = self.metadata.load())
                if (sid && sid == self.requested.load()) callback(sid, label);
        };
        api.motdata_Handler = [](uint8_t *data, int size, const char *, int, void *context) {
            auto &self = *static_cast<Receiver *>(context);
            const auto sid = self.selected.load();
            auto callback = self.image.load();
            if (callback && sid && sid == self.requested.load())
                callback(sid, data, size);
        };
        api.audioOut_Handler = [](int16_t *samples, int count, int rate, bool, void *context) {
            auto &self = *static_cast<Receiver *>(context);
            const auto selected = self.selected.load();
            if (samples && selected != 0 && selected == self.requested.load())
                self.audio(samples, count, rate);
        };
        api.programdata_Handler = [](audiodata *, void *) {};
        decoder = dabInit(this, &api, nullptr, nullptr, this);
    }

    ~Receiver() override { if (decoder) dabExit(decoder); }
    int32_t Samples() override { return iq.GetRingBufferReadAvailable(); }
    int32_t getSamples(std::complex<float> *out, int32_t count) override {
        return iq.getDataFromBuffer(out, count);
    }

    // The FIC updates and service lookups run on the decoder thread.
    void update_services() {
        if (!ensemble.empty())
            if (auto callback = metadata.load()) callback(0, ensemble.c_str());
        const auto wanted = requested.load();
        for (const auto &entry : names) {
            if (!is_audioService(decoder, entry.second))
                continue;
            audiodata data{};
            dataforAudioService(decoder, entry.second, data, 0);
            if (!data.defined || (data.ASCTy != 0 && data.ASCTy != 63))
                continue;
            if (!advertised.count(entry.first) || advertised[entry.first] != data.ASCTy) {
                advertised[entry.first] = data.ASCTy;
                station(entry.first, entry.second.c_str(), data.ASCTy);
            }
            if (wanted == entry.first && wanted != selected.load()) {
                selected.store(0);
                dabReset_msc(decoder);
                set_audioChannel(decoder, data);
                selected.store(wanted);
            }
        }
    }
};

extern "C" {
DAB_API void *falcon_dab_open(Station station, Audio audio, Reception reception) {
    try {
        auto *receiver = new Receiver(station, audio, reception);
        try { dabStartProcessing(receiver->decoder); }
        catch (...) { delete receiver; throw; }
        return receiver;
    } catch (...) { return nullptr; }
}

DAB_API void falcon_dab_close(void *handle) { delete static_cast<Receiver *>(handle); }

DAB_API int falcon_dab_push(void *handle, const void *samples, int count) {
    return static_cast<Receiver *>(handle)->iq.putDataIntoBuffer(samples, count);
}

DAB_API void falcon_dab_select(void *handle, uint32_t sid) {
    static_cast<Receiver *>(handle)->requested.store(sid);
}

DAB_API void falcon_dab_set_metadata(void *handle, Metadata callback) {
    static_cast<Receiver *>(handle)->metadata.store(callback);
}

DAB_API void falcon_dab_set_image(void *handle, Image callback) {
    static_cast<Receiver *>(handle)->image.store(callback);
}
}
