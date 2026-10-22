FROM docker.io/library/golang:1.24-bookworm@sha256:98d673f18a1aac43da744209873cb79323e11706f909251bcfb131828b95559d AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/models-enricher .
# 命名卷首次创建时继承镜像内挂载点的属主与权限；非 root 用户无需世界可写。
RUN install -d -m 0700 -o 65532 -g 65532 /out/cache

FROM gcr.io/distroless/static-debian12:nonroot@sha256:52dcfbabb7457ea47c82f6e13af8c8a4a1d9f7b0145142b3ecab20f2b888411d
COPY --from=build /out/models-enricher /models-enricher
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY config.yaml /app/config.yaml
COPY --from=build --chown=65532:65532 /out/cache /var/cache/models-enricher
USER 65532:65532
EXPOSE 8090
ENTRYPOINT ["/models-enricher"]
