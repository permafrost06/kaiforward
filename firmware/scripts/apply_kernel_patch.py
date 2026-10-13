#!/usr/bin/env python3
"""Apply the owned session-selection replacement to an inspected Nokia source tree."""
import argparse
import hashlib
import json
from pathlib import Path
from scripts.files import file_sha256
import shutil


def apply(kernel: Path, header: Path):
    source = kernel / 'sound/soc/msm/qdsp6v2/q6voice.c'
    text = source.read_text()
    directive = '#include "kaiforward_incall_playback.h"'
    target = source.with_name('kaiforward_incall_playback.h')
    if directive in text:
        if target.read_bytes() != header.read_bytes():
            raise ValueError('A different gateway replacement is already present')
        return {'already_applied': True}
    signature = 'int voc_start_playback(uint32_t set, uint16_t port_id)'
    if text.count(signature) != 1:
        raise ValueError('Expected exactly one Nokia voc_start_playback definition')
    start = text.index(signature)
    opening = text.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        if text[end] == '{':
            depth += 1
        elif text[end] == '}':
            depth -= 1
        end += 1
    previous = text[start:end]
    if hashlib.sha256(previous.encode()).hexdigest() != '7a9fade8d03ba0838c2e2dac972e8a41b3fd5f685d6e8a620fc8154792e60b5c':
        raise ValueError('Playback function differs from the pinned, inspected Nokia source')
    for required in ('VOICE_SESSION_NAME', 'VOICE2_SESSION_NAME', 'VOLTE_SESSION_NAME', 'music_info.count'):
        if required not in previous:
            raise ValueError('Original playback implementation differs from the inspected Nokia function')
    source.write_text(text[:start] + directive + text[end:])
    shutil.copyfile(header, target)
    return {'already_applied': False,
            'original_function_sha256': hashlib.sha256(previous.encode()).hexdigest(),
            'replacement_sha256': file_sha256(header)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('kernel', type=Path)
    parser.add_argument('--header', type=Path, default=Path(__file__).resolve().parents[1] / 'kernel/kaiforward_incall_playback.h')
    args = parser.parse_args()
    print(json.dumps(apply(args.kernel, args.header), indent=2))
