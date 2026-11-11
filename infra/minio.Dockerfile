# Community server publishes source releases; build the last security release
# directly instead of relying on a removed Docker Hub image.
FROM golang:1.24.8-alpine AS build
RUN apk add --no-cache git
ENV CGO_ENABLED=0 GOBIN=/out
RUN go install github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z

FROM alpine:3.22
RUN apk add --no-cache ca-certificates
COPY --from=build /out/minio /usr/local/bin/minio
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
