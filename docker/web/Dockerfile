ARG NODE_BUILD_IMAGE=node:24-alpine
ARG NGINX_RUNTIME_IMAGE=nginx:1.30.6-alpine
FROM ${NODE_BUILD_IMAGE} AS build
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
ARG SOURCE_REVISION=working-tree
RUN npm run build
RUN node --input-type=module -e "import fs from 'node:fs'; import crypto from 'node:crypto'; const r=process.env.SOURCE_REVISION; if(!/^(?:[a-f0-9]{40}|working-tree)$/.test(r)) throw new Error('Invalid revision'); fs.writeFileSync('dist/build-info.json',JSON.stringify({revision:r,release_source:r!=='working-tree',package_lock_sha256:crypto.createHash('sha256').update(fs.readFileSync('package-lock.json')).digest('hex')}))"

FROM ${NGINX_RUNTIME_IMAGE}
ARG SOURCE_REVISION=working-tree
LABEL org.opencontainers.image.revision=$SOURCE_REVISION \
    org.opencontainers.image.title=AutoScholar-Web \
    org.opencontainers.image.source=https://github.com/windskyther/AutoScholar
COPY docker/web/nginx.conf /etc/nginx/nginx.conf
COPY docker/web/server.conf docker/web/routes.conf /etc/nginx/autoscholar/
COPY --from=build /build/dist /usr/share/nginx/html
USER 101:101
EXPOSE 8080
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
HEALTHCHECK --interval=10s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:8080/health/live || exit 1
