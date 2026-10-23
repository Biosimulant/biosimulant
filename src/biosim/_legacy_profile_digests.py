"""Frozen identity hashes for envelopes written with catalogue 0.1.0.

These hashes preserve persisted identities; they do not grade scientific data.
Source: biosimulant_model_compatibility_standard-0.1.0-py3-none-any.whl,
SHA-256 e0d7a4896d7bebabf22a73f93f6804a3f4b6f5b6f96a5ef7292d1aa5185d9dc8
(the exact wheel pinned by the former dependency in uv.lock).
"""

LEGACY_PROFILE_DIGESTS: dict[str, str] = {
    "boltz.binding-probability/v1": "sha256:09579afeecb4088b02f6818f908e08aac6b4eb413df653020022f2eedf36eff3",
    "boltz.log10-ic50-micromolar/v1": "sha256:babfa4937822af31f85d0e3105371cb51f0182a52c600e7b55819d8b62b26137",
    "chemical.smiles/v1": "sha256:4c76c41b8435b2cad5f5fa7ce436eff0d2a138f2f92240755a1a7211c01e33bf",
    "protein-ligand.complex-structure-mmcif/v1": "sha256:f553a3fe3ee03224711a468e7588c0b01a8a9edc3cbed31e48e59f0ee1b02677",
    "protein.multiple-sequence-alignment/v1": "sha256:20d60fb7b2fe39e6c1db087d4ac80b0fdc6b7461328f917cce6768c87a9b3a52",
    "protein.sequence/v1": "sha256:3fd8da86aaadd6e330cd8aaa2fb674eaf90790a3ff0e6715264bda43a383b6f6",
}
