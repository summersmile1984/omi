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
COPY deploy/self-host/requirements.txt /tmp/fork-requirements.txt
RUN python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/fork-requirements.txt
COPY backend/fork/model_contract.py /opt/profile-build/backend/fork/model_contract.py
COPY scripts/profiles /opt/profile-build/scripts/profiles
COPY scripts/brand /opt/profile-build/scripts/brand
COPY brand /opt/profile-build/brand
COPY deploy/profiles /opt/profile-build/deploy/profiles
# Custom manifests live inside the build context and contain public identity only.
COPY ${BRAND_MANIFEST} /opt/profile-build/selected-brand.yaml
RUN cd /opt/profile-build && python scripts/profiles/render.py \
      --target self_hosted --manifest /opt/profile-build/selected-brand.yaml \
      --stage "${DEPLOYMENT_STAGE}" --emit-json > /app/fork/deployment_profiles.generated.json \
    && rm -rf /opt/profile-build /tmp/fork-requirements.txt
USER omi
CMD ["uvicorn", "fork.main:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "uvloop"]
