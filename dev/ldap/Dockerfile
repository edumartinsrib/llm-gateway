# OpenLDAP só para desenvolvimento: usuários e grupos de exemplo (ver seed.ldif).
FROM debian:bookworm-slim
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends slapd ldap-utils \
    && rm -rf /var/lib/apt/lists/* /var/lib/ldap/* /etc/ldap/slapd.d
COPY slapd.conf /etc/ldap/slapd.conf
COPY seed.ldif /seed.ldif
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 389
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
    CMD ldapsearch -x -H ldap://127.0.0.1 -b "" -s base >/dev/null 2>&1 || exit 1
CMD ["/entrypoint.sh"]
