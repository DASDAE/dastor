# dastor

**Experimental.** dastor is an early-stage, experimental package. It does nothing useful yet, and its API will change without notice. Don't depend on it.

dastor will be a server for distributed acoustic sensing (DAS) data, similar in spirit to FDSN web services, built on [DASCore](https://github.com/DASDAE/dascore). Planned features:

- Metadata queries backed by DASCore spools and their indexes.
- High-throughput data transfer over REST and streaming connections.
- Server-side, file-level access control.
- Direct file access over HTTP range requests, so a plain DASCore client can open a remote spool from an index.
