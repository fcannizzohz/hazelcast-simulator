FROM ubuntu:24.04

# Declare build arguments
ARG PYTHON_VERSION=3.11
ARG TARGETARCH

# Install runtime dependencies with retry mechanism
RUN apt-get update && apt-get install -y software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
        wget \
        python${PYTHON_VERSION} \
        python${PYTHON_VERSION}-distutils \
        python3-pip \
        openssh-client \
        rsync \
        git \
        curl \
        vim \
        less \
        unzip \
        wget \
        gnupg \
        lsb-release \
        ansible \
        dnsutils \
        iputils-ping \
        ca-certificates \
        apt-transport-https \
    && rm -rf /var/lib/apt/lists/*

# Install Eclipse Temurin JDK 17
RUN wget -qO - https://packages.adoptium.net/artifactory/api/gpg/key/public | gpg --dearmor -o /usr/share/keyrings/adoptium-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/adoptium-archive-keyring.gpg] https://packages.adoptium.net/artifactory/deb $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/adoptium.list && \
    apt-get update && apt-get install -y temurin-17-jdk && rm -rf /var/lib/apt/lists/*

# Set Temurin JDK 17 as the default Java version and configure JAVA_HOME
RUN JAVA_HOME_PATH=$(find /usr/lib/jvm -name "temurin-17*" -type d | head -1) && \
    update-alternatives --install /usr/bin/java java $JAVA_HOME_PATH/bin/java 1700 && \
    update-alternatives --install /usr/bin/javac javac $JAVA_HOME_PATH/bin/javac 1700 && \
    update-alternatives --set java $JAVA_HOME_PATH/bin/java && \
    update-alternatives --set javac $JAVA_HOME_PATH/bin/javac && \
    echo "export JAVA_HOME=$JAVA_HOME_PATH" >> /etc/environment && \
    echo "export JAVA_HOME=$JAVA_HOME_PATH" >> /etc/bash.bashrc

# Install Maven after JDK has been installed to avoid default JDK installation
RUN apt-get update && apt-get install -y maven && rm -rf /var/lib/apt/lists/*

# Install Terraform
RUN curl -fsSL https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/hashicorp.list && \
    apt-get update && apt-get install -y terraform && rm -rf /var/lib/apt/lists/*

# Install Kubernetes, Helm, and Google Cloud tooling for Kubernetes provisioners.
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y google-cloud-cli google-cloud-cli-gke-gcloud-auth-plugin && \
    rm -rf /var/lib/apt/lists/*

RUN KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable.txt)" && \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" && \
    chmod +x /usr/local/bin/kubectl

RUN HELM_VERSION="v3.15.4" && \
    curl -fsSLo /tmp/helm.tar.gz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" && \
    tar -xzf /tmp/helm.tar.gz -C /tmp && \
    mv /tmp/linux-${TARGETARCH}/helm /usr/local/bin/helm && \
    chmod +x /usr/local/bin/helm && \
    rm -rf /tmp/helm.tar.gz /tmp/linux-${TARGETARCH}

# Install the self-contained AWS CLI v2 so its Python dependencies cannot
# conflict with the versions pinned by Simulator.
RUN if [ "${TARGETARCH}" = "amd64" ]; then AWS_ARCH="x86_64"; \
    elif [ "${TARGETARCH}" = "arm64" ]; then AWS_ARCH="aarch64"; \
    else echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1; fi && \
    curl -fsSLo /tmp/awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# Install Python dependencies
RUN --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    python${PYTHON_VERSION} -m pip install --no-cache-dir --break-system-packages --ignore-installed -r /tmp/requirements.txt

# Create simulator directory structure
RUN mkdir -p /opt/simulator/lib /opt/simulator/drivers /opt/simulator/src /opt/simulator/templates /opt/simulator/examples /opt/simulator/conf /opt/simulator/playbooks /opt/simulator/observability /opt/simulator/bin /opt/simulator/user-lib

# Copy pre-built Java artifacts (lib and drivers directories)
COPY lib/ /opt/simulator/lib/
COPY drivers/ /opt/simulator/drivers/
COPY user-lib/ /opt/simulator/user-lib/

# The lib/ directory is local build output and can contain an older Simulator
# core JAR. Rebuild that JAR from the checked-in Java source so the runtime
# always includes coordinator behavior from this revision (for example,
# Kubernetes agent metadata handling).
COPY java/ /tmp/simulator-java/
RUN cd /tmp/simulator-java && \
    mvn -pl simulator clean package -DskipTests && \
    cp simulator/target/simulator-2.0-SNAPSHOT.jar /opt/simulator/lib/simulator-2.0-SNAPSHOT.jar && \
    rm -rf /tmp/simulator-java

# The checked-in source configuration is authoritative. The local drivers/
# directory contains generated build output and may predate source changes.
COPY java/drivers/driver-hazelcast4plus/conf/ /tmp/driver-hazelcast4plus-conf/
RUN for driver in driver-hazelcast4 driver-hazelcast-enterprise4 driver-hazelcast5 driver-hazelcast-enterprise5; do \
        cp -a /tmp/driver-hazelcast4plus-conf/. "/opt/simulator/drivers/${driver}/conf/"; \
    done && \
    rm -rf /tmp/driver-hazelcast4plus-conf

# Copy Python source code and configurations
COPY src/ /opt/simulator/src/
COPY templates/ /opt/simulator/templates/
COPY examples/ /opt/simulator/examples/
COPY conf/ /opt/simulator/conf/
COPY playbooks/ /opt/simulator/playbooks/
COPY observability/ /opt/simulator/observability/

# Copy bin directory containing Java executables (coordinator, agent, etc.)
COPY bin/ /opt/simulator/bin/

# Make all scripts executable after copying host scripts
RUN chmod +x /opt/simulator/bin/* && chmod +x /opt/simulator/bin/hidden/*

# Create container-optimized CLI wrapper scripts (after copying bin to avoid overwriting)
# Create perftest wrapper
RUN echo '#!/bin/bash' > /opt/simulator/bin/perftest && \
    echo 'cd /workspace' >> /opt/simulator/bin/perftest && \
    echo 'export SIMULATOR_HOME=/opt/simulator' >> /opt/simulator/bin/perftest && \
    echo 'export PYTHONPATH=/opt/simulator/src' >> /opt/simulator/bin/perftest && \
    echo 'export PATH="/opt/simulator/bin:$PATH"' >> /opt/simulator/bin/perftest && \
    echo "exec python${PYTHON_VERSION} /opt/simulator/src/perftest_cli.py \"\$@\"" >> /opt/simulator/bin/perftest

# Create inventory wrapper
RUN echo '#!/bin/bash' > /opt/simulator/bin/inventory && \
    echo 'cd /workspace' >> /opt/simulator/bin/inventory && \
    echo 'export SIMULATOR_HOME=/opt/simulator' >> /opt/simulator/bin/inventory && \
    echo 'export PYTHONPATH=/opt/simulator/src' >> /opt/simulator/bin/inventory && \
    echo "exec python${PYTHON_VERSION} /opt/simulator/src/inventory_cli.py \"\$@\"" >> /opt/simulator/bin/inventory

RUN echo '#!/bin/bash' > /opt/simulator/bin/iperf3test && \
    echo 'cd /workspace' >> /opt/simulator/bin/iperf3test && \
    echo 'export SIMULATOR_HOME=/opt/simulator' >> /opt/simulator/bin/iperf3test && \
    echo 'export PYTHONPATH=/opt/simulator/src' >> /opt/simulator/bin/iperf3test && \
    echo "exec python${PYTHON_VERSION} /opt/simulator/src/iperf3test_cli.py \"\$@\"" >> /opt/simulator/bin/iperf3test

RUN echo '#!/bin/bash' > /opt/simulator/bin/perfregtest && \
    echo 'cd /workspace' >> /opt/simulator/bin/perfregtest && \
    echo 'export SIMULATOR_HOME=/opt/simulator' >> /opt/simulator/bin/perfregtest && \
    echo 'export PYTHONPATH=/opt/simulator/src' >> /opt/simulator/bin/perfregtest && \
    echo "exec python${PYTHON_VERSION} /opt/simulator/src/perfregtest_cli.py \"\$@\"" >> /opt/simulator/bin/perfregtest

# Make wrapper scripts executable and create system-wide symlinks
RUN chmod +x /opt/simulator/bin/perftest /opt/simulator/bin/inventory /opt/simulator/bin/iperf3test /opt/simulator/bin/perfregtest && \
    ln -sf /opt/simulator/bin/perftest /usr/local/bin/perftest && \
    ln -sf /opt/simulator/bin/inventory /usr/local/bin/inventory

# Setup environment
ENV PATH="/opt/simulator/bin:$PATH"
ENV PYTHONPATH="/opt/simulator/src"
ENV SIMULATOR_HOME="/opt/simulator"

# Create workspace directory with proper permissions
RUN mkdir -p /workspace && chmod 777 /workspace
WORKDIR /workspace

# Create /tmp directory with proper permissions for user mapping
RUN chmod 777 /tmp

# Ensure the /opt/simulator directory is readable by all users
RUN chmod -R 755 /opt/simulator

# Add a welcome message script
RUN echo '#!/bin/bash' > /opt/simulator/bin/simulator-welcome && \
    echo 'echo "==========================================="' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  Hazelcast Simulator Environment"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "==========================================="' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo ""' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "Available commands:"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  perftest    - Performance testing CLI"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  inventory   - Infrastructure management"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  iperf3test  - Network performance testing"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  perfregtest - Performance regression testing"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo ""' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "Quick start:"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  perftest create myproject --template hazelcast5-ec2"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  cd myproject"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  inventory apply"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  inventory install java"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  inventory install simulator"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  perftest run"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo ""' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "Environment:"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  Java: $(java -version 2>&1 | head -n1)"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  Maven: $(mvn --version 2>&1 | head -n1)"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  Terraform: $(terraform version 2>&1 | head -n1)"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "  AWS CLI: $(aws --version 2>&1)"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo ""' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "Current directory: $(pwd)"' >> /opt/simulator/bin/simulator-welcome && \
    echo 'echo "==========================================="' >> /opt/simulator/bin/simulator-welcome

RUN chmod +x /opt/simulator/bin/simulator-welcome

# Verify artifacts were copied successfully
RUN echo "Verifying copied artifacts..." && \
    ls -la /opt/simulator/lib/ && \
    ls -la /opt/simulator/drivers/ && \
    ls -la /opt/simulator/user-lib/ && \
    echo "Pre-built artifact verification completed."

# Add health check to verify container readiness
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD java -version >/dev/null 2>&1 && \
      python3 --version >/dev/null 2>&1 && \
      which perftest >/dev/null 2>&1 && \
      which inventory >/dev/null 2>&1 || exit 1

# Set default command to show welcome and start interactive shell
CMD ["bash", "-c", "/opt/simulator/bin/simulator-welcome && exec bash"]

# Add metadata labels
LABEL maintainer="Hazelcast, Inc."
LABEL description="Hazelcast Simulator"
LABEL version="v2.0.0"
LABEL repository="https://github.com/hazelcast/hazelcast-simulator"
