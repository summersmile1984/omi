# The base is built from the unmodified upstream Dockerfile by build-images.sh.
ARG UPSTREAM_BACKEND_IMAGE
FROM ${UPSTREAM_BACKEND_IMAGE}

ARG OMI_SOURCE_GIT_COMMIT=unattributed
ARG OMI_SOURCE_GIT_TREE=unattributed
ARG DEPLOYMENT_STAGE=production
ARG BRAND_MANIFEST=brand/omi-upstream/manifest.yaml
LABEL com.omi.source.git-commit="${OMI_SOURCE_GIT_COMMIT}" \
      com.omi.source.git-tree="${OMI_SOURCE_GIT_TREE}"

USER root
COPY backend/requirements-fork.txt /tmp/fork-requirements.txt
RUN python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/fork-requirements.txt
# Retrieval counts tokens inside the isolated application network. Package the
# locked tokenizer's hash-checked vocabulary before switching to the runtime user.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
RUN python /app/scripts/prewarm_tiktoken_cache.py \
    && chmod 0555 /opt/tiktoken-cache && chmod 0444 /opt/tiktoken-cache/*
COPY backend/fork/model_contract.py /opt/profile-build/backend/fork/model_contract.py
COPY backend/fork/operator_ai.py /opt/profile-build/backend/fork/operator_ai.py
COPY scripts/profiles /opt/profile-build/scripts/profiles
COPY scripts/brand /opt/profile-build/scripts/brand
COPY brand /opt/profile-build/brand
COPY deploy/profiles /opt/profile-build/deploy/profiles
# Custom manifests live inside the build context and contain public identity only.
COPY ${BRAND_MANIFEST} /opt/profile-build/selected-brand.yaml
RUN cd /opt/profile-build && python scripts/profiles/render.py \
      --target self_hosted --manifest /opt/profile-build/selected-brand.yaml \
      --stage "${DEPLOYMENT_STAGE}" --emit-json > /app/fork/deployment_profiles.generated.json \
    && python scripts/brand/apply.py --manifest /opt/profile-build/selected-brand.yaml \
      --only firmware --output-root /opt/profile-build \
    && cp /opt/profile-build/backend/fork/firmware_brand.generated.json /app/fork/firmware_brand.generated.json \
    && rm -rf /opt/profile-build /tmp/fork-requirements.txt
USER omi
CMD ["uvicorn", "fork.main:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "uvloop"]
