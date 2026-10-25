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
# Vite copies public/ into dist/ as is. Without it the image had no
# favicon.ico, and /favicon.ico was answered with the SPA's index.html.
COPY public ./public
# tsc -b runs as part of `build`, so a type error fails the image build rather
# than shipping a broken bundle.
RUN pnpm run build


FROM nginx:1.27-alpine AS runtime

# A template, not a finished config: the official image's entrypoint renders
# /etc/nginx/templates/*.template into conf.d/ at start, substituting env vars.
# The filter limits that to API_* so nginx's own $host, $uri… are untouched.
ENV API_UPSTREAM=http://kvuno-api:80 \
    NGINX_ENVSUBST_FILTER=^API_
COPY nginx.conf /etc/nginx/templates/default.conf.template
# Not under conf.d/: everything there is included at http level, where these
# add_header lines would apply to (and be dropped by) every location.
COPY security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY api-proxy.conf /etc/nginx/snippets/api-proxy.conf
COPY --from=build /fe/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD wget --quiet --tries=1 --spider http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
