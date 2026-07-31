# TODO

## Reproducible container toolchain

- Add a checked-in toolchain manifest that pins the AWS CLI, Google Cloud CLI,
  GKE authentication plugin, kubectl, Helm, Terraform, and base-image versions.
- Make the Dockerfile install each tool from that manifest rather than tracking
  mutable upstream releases at image-build time.
- Emit the resolved tool versions in the image and expose a single command for
  inspecting them; have CI verify that the emitted versions match the manifest.
