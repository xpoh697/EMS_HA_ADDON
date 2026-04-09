ARG BUILD_FROM
FROM $BUILD_FROM

# Set shell
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install requirements for add-on
RUN apk add --no-cache \
    py3-pip \
    python3 \
    sqlite

# Copy data for add-on
COPY requirements.txt /
RUN pip3 install --no-cache-dir --break-system-packages -r /requirements.txt

# Copy root filesystem
COPY . /app
WORKDIR /app

# Ensure run.sh is executable
RUN chmod a+x run.sh

CMD [ "./run.sh" ]
