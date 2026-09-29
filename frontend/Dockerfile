# Frontend image for the React SPA.
#
# node exists only in the build stage; the runtime image is nginx plus the
# static bundle. This is the arrangement the repo had before ebb2871 removed
# the node tooling — but the old one shipped node_modules into the final
# image (~50 MB) to serve files from. Here the output is a few hundred KB.
FROM node:24-alpine AS build

WORKDIR /fe
RUN corepack enable && corepack prepare pnpm@12.4.0 --activate

# Manifests first, so `pnpm install` is cached until a dependency changes.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY tsconfig.json tsconfig.app.json tsconfig.node.json vite.config.ts index.html ./
COPY src ./src
# tsc -b runs as part of `build`, so a type error fails the image build rather
# than shipping a broken bundle.
RUN pnpm run build


FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /fe/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD wget --quiet --tries=1 --spider http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
