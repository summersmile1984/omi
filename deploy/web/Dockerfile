# Build context is the generated self_hosted artifact directory, not the repo.
FROM oven/bun:1.3.14-alpine
WORKDIR /app
COPY start.js ./start.js
COPY public ./public
ENV PORT=3000
EXPOSE 3000
CMD ["bun", "start.js"]
