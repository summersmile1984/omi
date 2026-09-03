FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436

ARG OMI_SOURCE_GIT_COMMIT=unattributed
ARG OMI_SOURCE_GIT_TREE=unattributed
LABEL com.omi.source.git-commit="${OMI_SOURCE_GIT_COMMIT}" \
      com.omi.source.git-tree="${OMI_SOURCE_GIT_TREE}"

ENV NODE_ENV=production
WORKDIR /app

COPY auth-server/package.json auth-server/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force

COPY auth-server/auth.ts ./auth.ts
COPY auth-server/src ./src

USER node
EXPOSE 3000
CMD ["node", "src/index.js"]
