# Initialize a Simulator Workspace

Complete this guide before following an AWS or Kubernetes example. It creates a
user-owned workspace outside the Simulator checkout. The checkout is needed
only if you choose to build a local image; every Simulator command in the
examples runs inside the selected Docker image.

## 1. Check local prerequisites

Docker must be installed and its daemon must be running:

```bash
docker version
docker info >/dev/null
```

Install the AWS CLI for AWS examples, and `gcloud`, `kubectl`, Helm, and
`gke-gcloud-auth-plugin` for GKE examples. The tutorials deliberately invoke
their image-resident counterparts through `docker-sim`, keeping Simulator
operations reproducible; the local clients remain available for credential
recovery and administrator workflows. Cloud credentials and kubeconfig state
are persisted in the standard host directories by the image-backed commands.

## 2. Select the Simulator image

Use the public image by default:

```bash
export SIM_IMAGE=hazelcast/simulator:latest
docker pull "$SIM_IMAGE"
```

The workspace launcher is available only in an image that includes this
workspace workflow. The extraction step below checks that requirement and tells
you to build `hazelcast/simulator:local` when the public image has not yet been
released with it.

To test unreleased changes, build a local image from the Simulator checkout
instead. After this one build command, continue with the same image-only
workflow:

```bash
docker build -t hazelcast/simulator:local /path/to/hazelcast-simulator
export SIM_IMAGE=hazelcast/simulator:local
```

## 3. Create and validate the workspace

Choose a location outside the checkout and create the required directories:

```bash
export SIMULATOR_WORKSPACE="$HOME/simulator-workspace"
mkdir -p "$SIMULATOR_WORKSPACE/bin" "$SIMULATOR_WORKSPACE/projects" "$HOME/.m2"
```

Simulator shares your existing Maven cache at `~/.m2`; it does not create a
second cache in the workspace. Verify that Docker can write through this mount
before using a tutorial. The probe removes the file that it creates:

```bash
docker run --rm \
  -v "$HOME/.m2:/root/.m2" \
  --entrypoint sh "$SIM_IMAGE" \
  -c 'test -r /root/.m2 && test -w /root/.m2 && touch /root/.m2/.simulator-mount-check && rm /root/.m2/.simulator-mount-check'
```

If this fails, create `~/.m2` with ownership that Docker can write, and, on
Docker Desktop, add your home directory to the file-sharing allowlist. Rerun
the probe until it succeeds.

## 4. Install the image-supplied launcher

Copy the launcher from the image into the workspace. This local file only
starts containers; it never mounts or runs Simulator code from the checkout.
If you opened a new terminal after step 3, set `SIMULATOR_WORKSPACE` again
before continuing:

```bash
test -d "$SIMULATOR_WORKSPACE/bin" && test -d "$SIMULATOR_WORKSPACE/projects" || {
  echo "Create the workspace directories in step 3 first" >&2
}
launcher_container=$(docker create "$SIM_IMAGE")
if ! docker cp "$launcher_container:/opt/simulator/bin/docker-sim" "${SIMULATOR_WORKSPACE:?}/bin/docker-sim"; then
  docker rm "$launcher_container" >/dev/null
  echo "The selected image does not contain the workspace launcher." >&2
  echo "Build hazelcast/simulator:local from this checkout, set SIM_IMAGE, and retry." >&2
fi
docker rm "$launcher_container" >/dev/null
chmod +x "$SIMULATOR_WORKSPACE/bin/docker-sim"
export PATH="$SIMULATOR_WORKSPACE/bin:$PATH"
```

Verify the installation and list the image templates:

```bash
docker-sim tutorial-init --help
docker-sim perftest create --list
```

## 5. Create a tutorial project

Each provider guide names the scenario to initialize. For example:

```bash
docker-sim tutorial-init <scenario> <project-name>
export PROJECT="$SIMULATOR_WORKSPACE/projects/<project-name>"
```

`docker-sim` rejects projects outside `$SIMULATOR_WORKSPACE/projects`. Keep
the same terminal open so `SIM_IMAGE`, `SIMULATOR_WORKSPACE`, `PATH`, and
`PROJECT` remain available. Set `HZ_LICENSEKEY` when an Enterprise scenario
requests it; do not commit that value to project files.

## 6. Cleanup rule
export SIMULATOR_WORKSPACE="${SIMULATOR_WORKSPACE:-$HOME/simulator-workspace}"

Every tutorial ends with `docker-sim inventory destroy`. Run it even after a
failed test when the tutorial created cloud resources, then use the provider
verification command in that tutorial to confirm that the owned resources were
removed. You may delete a project directory under `projects/` only after that
verification succeeds.
