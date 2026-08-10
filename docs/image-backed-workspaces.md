# Image-Backed Workspaces

## Why

Simulator projects need to survive container replacement, while the Simulator
CLI and its provider helpers need to match the image that runs them. The
image-backed workspace model separates those concerns: the image supplies the
tools and tutorial assets; the local workspace keeps projects and the shared
Maven cache.

## How it works

Install `docker-sim` in `<simulator-workspace>/bin` and keep projects below
`<simulator-workspace>/projects`. The launcher mounts the selected project as
`/workspace`, reuses `<simulator-workspace>/.m2` as the Maven cache when that
path exists, and persists AWS, Google Cloud, and Kubernetes client configuration
on the host. It rejects projects outside the workspace so a command cannot
accidentally mount an unrelated directory.

Use `docker-sim tutorial-init <scenario> <project>` to create an image-bundled
tutorial project. Set `PROJECT` to that project before running Simulator
commands. The launcher also runs image-resident cloud and Kubernetes tools, so
tutorials do not depend on helper files from this checkout.

## Before using it

Docker must be available, and the selected Maven cache directory must exist and
be readable and writable by the user running Docker. The default cache is
`<simulator-workspace>/.m2` when present, otherwise `~/.m2`; set `SIMULATOR_M2`
to override it. Cloud credentials remain in their normal host locations and are
mounted only when the launcher runs.

Follow the [workspace initialization tutorial](../examples/README_INIT.md) for
the installation, cache probe, image selection, and cleanup rule.
