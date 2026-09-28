FROM alpine:3.20

RUN apk add --no-cache squid

CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
