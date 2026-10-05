#!/bin/sh
# Sobe o slapd, carrega os dados de exemplo na primeira vez (com o overlay memberof
# ativo, para o atributo memberOf ser preenchido) e fica em primeiro plano.
set -e
mkdir -p /var/run/slapd /var/lib/ldap
slapd -h "ldap:///" -f /etc/ldap/slapd.conf -d 0 &
PID=$!
for i in $(seq 1 50); do
  ldapsearch -x -H ldap://127.0.0.1 -b "" -s base >/dev/null 2>&1 && break
  sleep 0.2
done
if ! ldapsearch -x -H ldap://127.0.0.1 -D cn=admin,dc=exemplo,dc=org -w admin \
     -b dc=exemplo,dc=org -s base >/dev/null 2>&1; then
  ldapadd -x -H ldap://127.0.0.1 -D cn=admin,dc=exemplo,dc=org -w admin -f /seed.ldif
  echo "dados de exemplo carregados"
fi
wait $PID
