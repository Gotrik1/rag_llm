FROM node:22-bookworm-slim AS ui-builder
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY ui ./
RUN npm run build

FROM nginx:1.28-alpine
COPY deploy/nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=ui-builder /ui/dist /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf && \
    chown -R nginx:nginx /usr/share/nginx/html /etc/nginx/templates /etc/nginx/conf.d
USER nginx
EXPOSE 8080
