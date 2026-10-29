// Reference ATSC 8-VSB transmitter: MPEG-TS in, complex64 baseband out.
//
// Wraps the philburr/atsc modulator library (randomizer -> RS(207,187) -> interleaver ->
// trellis -> field sync -> RRC, pilot added as a +1.25 DC offset). One output sample per
// symbol at the ATSC symbol rate, 4.5e6/286*684 = 10.762237 MS/s.
//
// Why this exists: gr-dtv 3.10 ships an ATSC *receiver* but no transmitter, and its
// `dvbs2_modulator_bc(MOD_8VSB)` is only an 8PSK symbol map - composing a transmitter out
// of the gr-dtv sub-blocks produces a signal that looks plausible and never locks. This
// harness drives a real modulator instead, so `atsc_probe.py --loopback` proves the
// receive chain rather than assuming it.
//
// Built by atsc_probe.build_reference_tx(); the library is cloned to ATSC_REF_TX_DIR.
#include <complex>
#include <cstdio>
#include <cstring>
#include <functional>
#include <memory>

#include "atsc/atsc.h"
#include "common/atsc_parameters.h"

static constexpr double kSymbolRate = 4.5e6 / 286 * 684;

int main(int argc, char* argv[]) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s input.ts output.cfile\n", argv[0]);
        return 2;
    }
    FILE* input = fopen(argv[1], "rb");
    if (!input) { fprintf(stderr, "cannot open %s\n", argv[1]); return 2; }
    FILE* out = fopen(argv[2], "wb");
    if (!out) { fprintf(stderr, "cannot open %s\n", argv[2]); fclose(input); return 2; }

    auto encoder = atsc::atsc_encoder::create();
    if (!encoder) { fprintf(stderr, "encoder create failed\n"); return 2; }

    auto field = std::make_unique<atsc_field_mpeg2>();
    unsigned fields = 0;
    unsigned long long samples = 0;
    while (fread(field->data(), ATSC_DATA_SEGMENTS * ATSC_MPEG2_BYTES, 1, input) == 1) {
        encoder->process(field->data(), ATSC_DATA_SEGMENTS,
                         [&](void* p, unsigned bytes) {
                             fwrite(p, 1, bytes, out);
                             samples += bytes / sizeof(std::complex<float>);
                             fields++;
                         });
    }
    fclose(input);
    fclose(out);
    printf("fields=%u samples=%llu rate=%.0f Hz duration=%.3f s bytes=%llu\n",
           fields, samples, kSymbolRate, samples / kSymbolRate,
           samples * sizeof(std::complex<float>));
    return 0;
}
