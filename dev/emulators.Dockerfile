# Omi Firebase emulator suite image: Node 22 + JRE 17 + firebase-tools 13.35.1
# NOTE: firebase-tools >=15 requires JDK 21; Debian bookworm only ships JDK 17,
#       so we pin 13.35.1 (last line with JDK 17 support).
# Build: docker build -f dev/emulators.Dockerfile -t omi-emulators:local dev/
# Run:   docker run --rm -p 8080:8080 -p 9099:9099 -p 9199:9199 \
#          -v $(pwd)/dev/firebase.json:/srv/firebase/firebase.json:ro omi-emulators:local
FROM node:22-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g firebase-tools@13.35.1

WORKDIR /srv/firebase
EXPOSE 8080 9099 9199

ENTRYPOINT ["firebase", "emulators:start", "--only", "firestore,auth,storage", "--project", "demo-omi-local"]
