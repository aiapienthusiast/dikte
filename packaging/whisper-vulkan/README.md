# The Vulkan whisper-server bundle

whisper.cpp publishes a CPU-only archive for Linux, and CPU, BLAS and CUDA
ones for Windows, so most graphics cards are out of reach through the Download
button. This directory builds the archives upstream does not: `whisper-server`
with a dynamic Vulkan backend next to the CPU ones, for x86_64, against the
Ubuntu 22.04 runtime contract, and for Windows x64 with `windows_bundle.py`.

It is published as a release of Dikte's own, `whisper.cpp-v<version>`, marked
as a prerelease and kept off Latest so that neither the update check nor the
download page picks it up. `dikte/ggml.py` fetches it by tag and installs it
only when the archive's digest is the reviewed one; anything else falls back
to upstream's CPU archive, and the settings window says when it did.

## Publishing a new bundle

1. Enable GitHub's immutable releases setting for the repository, and give the
   `dependency-release` environment a required reviewer. Both are repository
   settings, not something this workflow can do for itself.
2. Run **whisper.cpp Vulkan bundle** on `master` with the new version and its
   peeled commit, `expected_sha256` and `expected_windows_sha256` empty and
   `publish: false`. The run builds both archives and reports their digests;
   without reviewed digests it refuses to publish, which is what the first run
   is for.
3. Review those digests against builds of your own, then run the workflow
   again with the same version and commit, both digests set, and
   `publish: true`. Approve the environment when it asks.
4. Write the same version, tag and digest into `MANAGED_WHISPER_RELEASE`,
   `MANAGED_WHISPER_VERSION` and `MANAGED_WHISPER_SHA256` in `dikte/ggml.py`,
   and into `REVIEWED_WHISPER_VERSION` and `REVIEWED_WHISPER_SHA256` in the
   workflow; the Windows ones go into the `MANAGED_WHISPER_WINDOWS_*` and
   `REVIEWED_WHISPER_WINDOWS_*` names beside them. Until the Windows digest is
   filled in, it is a placeholder and every Windows machine gets upstream's
   build, which is also why README.windows.md still says there is no GPU
   build to fetch. `tests/test_packaging.py` holds the two sides together.
5. Ship a Dikte release. Until one goes out, nobody's Dikte knows the new
   bundle exists.

## What this costs, and what it does not promise

The digest lives in Dikte's source, so a backend update is a Dikte release.
Linux x86_64 machines with a Vulkan loader stay on the pinned whisper.cpp
version until step 5 happens, while every other platform follows upstream's
newest release on its own. That is the deliberate trade: an executable Dikte
downloads is not allowed to change without a reviewed digest behind it.

The build is deterministic between two runs of the same builder, not across
time. The base image, the CMake tarball, the LunarG packages and the direct
apt packages are pinned by digest or version, but the Ubuntu and LunarG
repository metadata behind them is not, and LunarG drops superseded packages.
A rebuild months later can fail to resolve, or resolve to something that
produces a different digest. Treat the published archive as the artifact, not
as something reproducible on demand: a version bump means building,
validating, reviewing the new digest and updating the pinned tuple together.

Windows is weaker still: an update of the `windows-latest` image can change
the digest, and its runner has no GPU, so somebody with a card has to try the
Vulkan path before a digest is reviewed. `windows_bundle.py` says why.
