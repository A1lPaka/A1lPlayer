# CUDA wheelhouse

Optional local/offline CUDA runtime package source.

A1lPlayer does not bundle CUDA wheels by default. If this folder contains
compatible `.whl` files, the CUDA runtime installer uses it with
`pip --no-index --find-links` instead of downloading packages from the online
index.

Leave this folder empty for the normal online install flow. Wheel files are
ignored by git because they are large runtime payloads.
