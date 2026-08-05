# Platform update TUF fixtures

The platform-update trust tests generate Ed25519 keys and TUF metadata in
temporary directories for every run. No reusable private signing keys or
mutable repository metadata are checked into this directory.

The generated repositories cover root rotation, metadata expiry, replay,
snapshot mix-and-match, target digest mismatch, and exact target retrieval.
