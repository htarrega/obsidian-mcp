# Despliegue en Oracle Cloud (Always Free, Ampere A1)

Despliegue como código de **obsidian-mcp** en una VM Oracle Cloud Always Free
(Ampere A1, Ubuntu 24.04 aarch64), con Obsidian Headless (`ob`) manteniendo el
vault sincronizado vía Obsidian Sync y Caddy delante con TLS automático.

- URL pública: `https://obsidian.mcp.htarrega.me`
- Conector MCP (claude.ai → Settings → Connectors): `https://obsidian.mcp.htarrega.me/mcp`
- Callback OAuth de GitHub: `https://obsidian.mcp.htarrega.me/auth/callback`
- Login de GitHub autorizado: `htarrega`

El dominio es configurable (`DOMAIN` / `--domain`) en todos los scripts y plantillas.

> Diagramas de la arquitectura: [docs/arquitectura.md](../docs/arquitectura.md)

## Arquitectura

```
  claude.ai / Claude Code / Desktop
              │  HTTPS 443 (TLS Let's Encrypt, DNS-only en Cloudflare)
              ▼
 ┌──────────────────────── VM Oracle A1 (Ubuntu 24.04 arm64) ────────────────────────┐
 │  OCI Security List: 22, 80, 443   ─►  iptables INPUT (ACCEPT antes del REJECT)    │
 │                                                                                    │
 │  caddy.service  :80/:443  ── reverse_proxy (flush_interval -1) ──►  127.0.0.1:8000 │
 │                                                                         │          │
 │  obsidian-mcp.service (User=obsidian, sandbox systemd)                  ▼          │
 │   └─ /opt/obsidian-mcp/app/.venv/bin/obsidian-remote-mcp   (TRANSPORT=http)        │
 │        ├─ OAuth GitHub (FASTMCP_HOME=/var/lib/obsidian/mcp-data)                    │
 │        ├─ índice + watcher (inotify) sobre /var/lib/obsidian/vault                  │
 │        └─ hijo supervisado: ob sync --continuous  (ENABLE_OB=true)                  │
 │                 └─ credenciales/logs: /var/lib/obsidian/.config/obsidian-headless   │
 └──────────────────────────────────────────────────────────────────┬─────────────────┘
                                                                    │ HTTPS saliente
                                                                    ▼
                                                     Obsidian Sync (E2E) ◄─► tus dispositivos
```

No hay unidad systemd separada para `ob`: el propio servidor MCP lanza y
supervisa `ob sync --continuous` (con backoff hasta `OB_RESTART_BACKOFF_MAX`).
`KillMode=mixed` hace que al parar el servicio el SIGTERM vaya al proceso
Python (que cierra a `ob`), y lo que quede vivo tras 30 s recibe SIGKILL.

### Rutas en la VM

| Ruta | Dueño / modo | Contenido |
|---|---|---|
| `/opt/obsidian-mcp/app` | root, 0755 | `git clone` en un commit concreto (detached) + `.venv` |
| `/opt/obsidian-mcp/python` | root, 0755 | CPython gestionado por uv (`UV_PYTHON_INSTALL_DIR`) |
| `/opt/obsidian-mcp/DEPLOYED_COMMIT`, `PREVIOUS_COMMIT` | root | commit activo y anterior (rollback) |
| `/etc/obsidian-mcp/env` | root:root 0600 (directorio root:root 0700) | configuración y secretos (`EnvironmentFile`) |
| `/var/lib/obsidian` | obsidian 0750 | HOME del usuario `obsidian` (shell `nologin`) |
| `/var/lib/obsidian/vault` | obsidian 0700 | el vault sincronizado |
| `/var/lib/obsidian/mcp-data` | obsidian 0700 | `FASTMCP_HOME`: tokens OAuth, locks, audit log |
| `/var/lib/obsidian/.config/obsidian-headless` | obsidian 0700 | credenciales de `ob`, `sync/<id>/sync.log` |

## Ficheros de este directorio

| Fichero | Destino en la VM |
|---|---|
| `provision.sh` | se ejecuta una vez (idempotente, se puede repetir) |
| `update.sh` | despliegue de otro commit / `rollback` |
| `obsidian-mcp.service` | `/etc/systemd/system/obsidian-mcp.service` |
| `env.example` | `/etc/obsidian-mcp/env` (solo si no existe) |
| `Caddyfile.template` | `/etc/caddy/Caddyfile` (renderizado con `DOMAIN`) |
| `sysctl-obsidian.conf` | `/etc/sysctl.d/60-obsidian.conf` (límites inotify) |
| `sshd-hardening.conf` | `/etc/ssh/sshd_config.d/00-obsidian-hardening.conf` |
| `logrotate-obsidian-headless` | `/etc/logrotate.d/obsidian-headless` |
| `fail2ban-sshd.local` | `/etc/fail2ban/jail.d/obsidian-sshd.local` |
| `fail2ban-caddy-oauth.conf` | `/etc/fail2ban/filter.d/obsidian-caddy-oauth.conf` (filtro sobre el access log JSON de Caddy) |
| `fail2ban-caddy-oauth.local` | `/etc/fail2ban/jail.d/obsidian-caddy-oauth.local` (jails `obsidian-oauth-register` y `obsidian-oauth-authorize`) |
| `oauth-store-usage.sh` | `/usr/local/sbin/obsidian-mcp-oauth-usage` |
| `obsidian-mcp-oauth-usage.{service,timer}` | `/etc/systemd/system/` (aviso semanal si `FASTMCP_HOME` > 200 MB) |

## Recursos OCI

La creación de la VM, VCN, subred, Security List (ingress TCP 22/80/443) e IP
pública reservada se documenta en [`oci-resources.md`](oci-resources.md)
*(pendiente: lo rellena otro paso)*.

## Versiones fijadas

| Componente | Versión | Dónde se fija |
|---|---|---|
| obsidian-headless (`ob`) | `0.0.14` | `OB_VERSION` / `--ob-version` |
| Node.js | 22.x LTS (NodeSource, arm64) | `NODE_MAJOR` / `--node-major` |
| uv | `0.11.7` | `UV_VERSION` / `--uv-version` |
| Python | 3.12 (último patch gestionado por uv; admite `3.12.x` exacto) | `PYTHON_VERSION` |
| Dependencias Python | las de `uv.lock` (`uv sync --locked`) | commit desplegado |
| obsidian-mcp | commit SHA de 40 hex (nunca rama/tag) | `COMMIT` / `--commit` |
| Caddy | `2.11.9` (apt, repo Cloudsmith `caddy/stable`; `latest` = sin fijar) | `CADDY_VERSION` / `--caddy-version` |
| Claves APT | huellas fijadas en `provision.sh` (`CADDY_KEY_FPR`, `NODESOURCE_KEY_FPR`); se aborta si la clave descargada no coincide | `provision.sh` |

## Requisitos previos

1. VM Ampere A1 con Ubuntu 24.04 creada, IP pública y Security List con ingress
   TCP 22, 80 y 443 (ver `oci-resources.md`).
2. DNS en Cloudflare: registro **A** `obsidian.mcp` → IP pública, **DNS only**
   (nube gris). Con proxy naranja Caddy no puede emitir el certificado como está
   configurado y el streaming se comporta distinto.
3. Alias SSH en tu máquina (`~/.ssh/config`):

   ```
   Host obsidian-vps
       HostName <IP pública>
       User ubuntu
       IdentityFile ~/.ssh/<tu_clave>
   ```

   Comprueba que entras con clave **antes** de provisionar: el script desactiva
   `PasswordAuthentication` y `PermitRootLogin`.
4. Una OAuth App de GitHub (GitHub → Settings → Developer settings → OAuth Apps):
   - Homepage URL: `https://obsidian.mcp.htarrega.me`
   - Authorization callback URL: `https://obsidian.mcp.htarrega.me/auth/callback`
5. Cuenta de Obsidian con Sync y la contraseña de cifrado E2E del vault remoto.
6. El SHA del commit a desplegar (que esté pusheado a `htarrega/obsidian-mcp`):
   `git rev-parse HEAD`.

## Despliegue paso a paso

### 1. Copiar `deploy/` a la VM

```bash
scp -r deploy obsidian-vps:~/deploy
# o en la VM: git clone https://github.com/htarrega/obsidian-mcp ~/src && cp -r ~/src/deploy ~/deploy
```

### 2. Provisionar

```bash
COMMIT=$(git rev-parse HEAD)   # en tu máquina, en el repo
ssh -t obsidian-vps "sudo ~/deploy/provision.sh --commit $COMMIT"
# opciones: --domain, --acme-email tu@correo, --skip-upgrade, --no-start, --ssh-ports 22
```

Usa los flags en vez de `sudo COMMIT=... ./provision.sh`: `sudo` filtra el
entorno y solo deja pasar variables en la línea de órdenes si la regla de
sudoers es `ALL` o tiene `SETENV` (en Oracle sí lo es, pero los flags no
dependen de ello).

Qué hace (idempotente): `apt upgrade`; paquetes base; unattended-upgrades;
`sshd` sin contraseñas (verificado con `sshd -T`); reglas iptables 80/443 (y
puertos SSH) insertadas **antes** del `REJECT` de Oracle y guardadas con
`netfilter-persistent`; límites inotify; usuario `obsidian` y directorios; Node
22 + `obsidian-headless@0.0.14`; uv + CPython en `/opt/obsidian-mcp/python`;
clon del repo en el commit pedido + `uv sync --locked --no-dev`;
`/etc/obsidian-mcp/env` desde `env.example` (solo si no existe); unidad systemd
habilitada; Caddy desde el repo oficial (Cloudsmith) con `caddy validate`;
logrotate de `sync.log`; jails de fail2ban `sshd` y de los endpoints OAuth
sin autenticar (`POST /register`, `GET /authorize`, leídos del access log JSON
de Caddy); timer semanal que avisa en el journal si `FASTMCP_HOME` supera
200 MB (no borra nada). Al final imprime un resumen
con versiones y commit.

La primera vez el servicio **no arranca**: el env todavía tiene `RELLENAR`.

### 3. Obsidian Headless: login, vault y primera sincronización

Todos los comandos se ejecutan como el usuario `obsidian` con el mismo `HOME` y
`XDG_CONFIG_HOME` que usa la unidad systemd, para que las credenciales queden en
`/var/lib/obsidian/.config/obsidian-headless`.

> **¿Hace falta `-s /bin/bash` por ser `nologin`?** No. `sudo -u obsidian cmd`
> ejecuta `cmd` directamente (execve), sin pasar por la shell de login del
> usuario; la shell solo importa con `sudo -i`, `sudo -s` o `su -`. `-H` y el
> `env HOME=... XDG_CONFIG_HOME=...` explícito evitan depender de la política de
> entorno de sudo (que descartaría `XDG_CONFIG_HOME`).

Login (pide email, contraseña y 2FA de Obsidian):

```bash
ssh -t obsidian-vps 'sudo -u obsidian -H env HOME=/var/lib/obsidian XDG_CONFIG_HOME=/var/lib/obsidian/.config ob login'
```

Listar los vaults remotos:

```bash
ssh -t obsidian-vps 'sudo -u obsidian -H env HOME=/var/lib/obsidian XDG_CONFIG_HOME=/var/lib/obsidian/.config ob sync-list-remote'
```

Vincular el vault remoto con el directorio local (pide la contraseña E2E):

```bash
ssh -t obsidian-vps 'sudo -u obsidian -H env -C /var/lib/obsidian/vault HOME=/var/lib/obsidian XDG_CONFIG_HOME=/var/lib/obsidian/.config ob sync-setup --vault "<NOMBRE>" --path /var/lib/obsidian/vault --device-name oracle-vps'
```

> Nota: en lugar de `cd /var/lib/obsidian/vault && sudo -u obsidian ...` se usa
> `env -C` (coreutils ≥ 8.28) porque el vault es `0700` del usuario `obsidian`
> y el usuario `ubuntu` no puede hacer `cd` dentro; `env -C` cambia de
> directorio ya como `obsidian`.

Primera sincronización completa, en primer plano (con el servicio parado para
que no haya dos `ob` a la vez sobre el mismo vault):

```bash
ssh -t obsidian-vps 'sudo systemctl stop obsidian-mcp; sudo -u obsidian -H env HOME=/var/lib/obsidian XDG_CONFIG_HOME=/var/lib/obsidian/.config ob sync --path /var/lib/obsidian/vault'
ssh obsidian-vps "sudo find /var/lib/obsidian/vault -name '*.md' | wc -l"
```

El número de notas debe coincidir (aprox.) con el de tu vault en el escritorio.

### 4. Secretos

```bash
ssh -t obsidian-vps sudoedit /etc/obsidian-mcp/env
```

Rellena `OAUTH_GITHUB_CLIENT_ID` y `OAUTH_GITHUB_CLIENT_SECRET` (sustituyendo
`RELLENAR`); revisa `PUBLIC_BASE_URL`, `OAUTH_GITHUB_ALLOWED_LOGINS` y la
política de escritura. El fichero no se versiona ni lo sobrescribe
`provision.sh`.

### 5. Arrancar y mirar logs

```bash
ssh obsidian-vps 'sudo systemctl start obsidian-mcp && systemctl status obsidian-mcp --no-pager'
ssh obsidian-vps 'journalctl -u obsidian-mcp -f'           # Ctrl-C para salir
ssh obsidian-vps 'journalctl -u caddy -n 100 --no-pager'   # emisión del certificado
```

(Volver a ejecutar `provision.sh` con el env ya completo también arranca/reinicia
el servicio y espera a `/health`.)

### 6. Conectar claude.ai

Settings → Connectors → Add custom connector → URL
`https://obsidian.mcp.htarrega.me/mcp` → login con GitHub (`htarrega`).

## Verificación

Desde tu máquina:

```bash
D=obsidian.mcp.htarrega.me
curl -fsS https://$D/health                                   # {"status":"ok",...}
curl -fsS https://$D/.well-known/oauth-protected-resource/mcp  # metadata del recurso
curl -fsS https://$D/.well-known/oauth-authorization-server    # metadata del AS (issuer = https://$D)
curl -si -X POST https://$D/mcp -H 'content-type: application/json' -d '{}' | head -n 15
#   -> HTTP/2 401 + WWW-Authenticate: Bearer ... resource_metadata=...
curl -sI https://$D/health | grep -iE '^(server|strict-transport|x-content-type|referrer)'
#   -> sin cabecera Server; HSTS, nosniff y no-referrer presentes
nmap -Pn -p 1-65535 --open $D        # solo 22, 80, 443 (o: nc -zv $D 8000 debe fallar)
```

En la VM:

```bash
sudo ss -tlnp            # 8000 (python) y 2019 (caddy admin) SOLO en 127.0.0.1; 80/443 caddy; 22 sshd
sudo iptables -L INPUT -n --line-numbers   # ACCEPT 80/443 por encima del REJECT
sudo sshd -T | grep -E 'passwordauthentication|permitrootlogin'
sudo fail2ban-client status sshd
sudo fail2ban-client status obsidian-oauth-register    # >20 POST /register en 10 min -> ban 1 h
sudo fail2ban-client status obsidian-oauth-authorize   # >60 GET /authorize en 10 min -> ban 1 h
sudo fail2ban-regex /var/log/caddy/obsidian-mcp-access.log \
  '/etc/fail2ban/filter.d/obsidian-caddy-oauth.conf[oauth_method=POST, oauth_path=/register]'
systemctl list-timers obsidian-mcp-oauth-usage.timer
journalctl -u obsidian-mcp-oauth-usage -n 5     # tamaño de FASTMCP_HOME (solo avisa, nunca borra)
systemd-analyze security obsidian-mcp      # exposición ~2.9 "OK"
sysctl fs.inotify.max_user_watches fs.inotify.max_user_instances
pgrep -u obsidian -a                         # python + node (ob sync --continuous)
sudo logrotate -d /etc/logrotate.d/obsidian-headless
```

Checklist:

- [ ] `/health` 200 por HTTPS con certificado válido.
- [ ] `/mcp` sin token → 401 con `WWW-Authenticate`.
- [ ] Puerto 8000 no accesible desde fuera.
- [ ] Login en claude.ai con `htarrega` funciona; otra cuenta de GitHub es rechazada.
- [ ] Crear una nota desde Claude → aparece en el escritorio vía Sync (y viceversa).
- [ ] `systemctl restart obsidian-mcp` no deja procesos `ob` huérfanos (`pgrep -u obsidian -a`).

## Actualizar y hacer rollback

```bash
# en tu máquina, tras pushear:
NEW=$(git rev-parse HEAD)
ssh -t obsidian-vps "sudo ~/deploy/update.sh $NEW"   # fetch, checkout --detach, uv sync --locked, restart, /health
ssh -t obsidian-vps 'sudo ~/deploy/update.sh rollback' # vuelve a /opt/obsidian-mcp/PREVIOUS_COMMIT
ssh obsidian-vps 'cat /opt/obsidian-mcp/DEPLOYED_COMMIT /opt/obsidian-mcp/PREVIOUS_COMMIT'
```

`update.sh` solo cambia la aplicación. Si cambian la unidad, el Caddyfile,
`ob` o versiones de Node/uv: copia el `deploy/` nuevo y vuelve a lanzar
`provision.sh --commit <sha> --skip-upgrade` (no toca `/etc/obsidian-mcp/env`).
Si `uv sync` falla, `update.sh` restaura el commit anterior antes de salir.

Cambiar `ob`: `--ob-version x.y.z` en `provision.sh` y reiniciar el servicio.

## Ajustes de política

**Restringir la escritura a una carpeta** (`WRITE_PATHS`): vacío = todo el vault
menos `DENY_WRITE_PATHS`. Con barra final la regla incluye la carpeta y todo lo
que cuelga; sin barra, solo esa ruta exacta. Varias reglas separadas por comas:

```bash
ssh -t obsidian-vps sudoedit /etc/obsidian-mcp/env   # WRITE_PATHS=Inbox/,AI-Output/
ssh obsidian-vps 'sudo systemctl restart obsidian-mcp'
```

`DENY_WRITE_PATHS` **reemplaza** la lista por defecto (no la amplía): mantén
`.obsidian/` (config de Obsidian y `.obsidian/.sync.lock` de `ob`), `.trash/`
y `_AI_INSTRUCTIONS.md` además de lo que añadas.

**Solo lectura temporal**: `READ_ONLY=true` y reiniciar.

**`ENABLE_OB_ADMIN`**: déjalo en `false`. Actívalo solo de forma puntual para
cambiar la configuración de sync sin SSH (carpetas excluidas, tipos de fichero,
categorías de config, estrategia de conflictos) y vuelve a `false` al terminar:
son cambios persistentes y cualquier sesión MCP autenticada podría usarlos. Se
ignora si `READ_ONLY=true`. Con `ENABLE_OB=true` siempre están `sync_status`,
`sync_logs`, `ob_info` y `list_remote_vaults`; `ENABLE_OB_OPS=true` añade
`sync_restart`. Login, logout, setup y unlink nunca se exponen.

**Herramientas de alto impacto** (`ENABLE_DELETE`, `ENABLE_MOVE`,
`ENABLE_FOLDER_RENAME`, `ENABLE_BULK_REPLACE`): desactivadas; cualquier cambio se
propaga a todos tus dispositivos por Sync. Si activas alguna, ten copia/historial
de versiones de Obsidian Sync a mano.

## Oracle Always Free: avisos

- **Reclamación por inactividad**: Oracle puede detener (y reclamar) instancias
  Always Free consideradas *idle* si durante 7 días el percentil 95 de CPU queda
  por debajo del 20 % (y la red y, en A1, la memoria también < 20 %). Un servidor
  MCP personal está casi siempre ocioso, así que es un riesgo real.
- **Pay-As-You-Go**: pasar la cuenta a PAYG (con tarjeta) elimina esa
  reclamación de instancias idle y mantiene gratis lo que esté dentro de los
  límites Always Free (A1: 4 OCPU / 24 GB en total). Pon una alerta de
  presupuesto (p. ej. 1 €) para detectar cualquier recurso de pago por error.
- Haz copia de `/etc/obsidian-mcp/env` y, si quieres conservar las sesiones
  OAuth, de `/var/lib/obsidian/mcp-data`. El vault se recupera de Obsidian Sync.

## Pendiente (endurecimiento de la cadena de suministro)

- **Huella de la clave de NodeSource**: NodeSource no la publica en su
  documentación. `NODESOURCE_KEY_FPR` es la de la clave servida por
  `deb.nodesource.com`, que coincide con la de `keyserver.ubuntu.com`
  (2026-11-08). Confirmarla contra una fuente oficial si aparece. La de Caddy
  está contrastada con la API de Cloudsmith.
- **obsidian-headless (npm)**: `npm install -g obsidian-headless@x.y.z` fija la
  versión pero no la integridad del tarball ni de sus dependencias
  transitivas. Pendiente: instalar desde un `package-lock.json` versionado
  aquí (`npm ci` en un prefijo propio) o comprobar `dist.integrity` (sha512) del
  tarball antes de instalarlo.
- **uv**: el instalador `https://astral.sh/uv/<versión>/install.sh` se ejecuta
  sin comprobar checksum. Pendiente: descargar el binario
  `uv-aarch64-unknown-linux-gnu.tar.gz` de la release de GitHub y verificar su
  `.sha256` (o fijar el sha256 en `provision.sh`) antes de instalarlo.

## Problemas habituales

- **Caddy no obtiene certificado**: DNS no es *DNS only*, o faltan 80/443 en la
  Security List o en iptables (`sudo iptables -L INPUT -n --line-numbers`).
- **`ob` falla con ENOSPC / no detecta cambios**: límites inotify (`sysctl
  fs.inotify.max_user_watches`).
- **`npm install -g obsidian-headless` falla compilando `better-sqlite3`**:
  faltan `build-essential`/`python3` (los instala `provision.sh`); revisa la
  salida de node-gyp.
- **`ob` no está en `/usr/bin/ob`**: `provision.sh` avisa; ajusta `OB_BIN` en el env.
- **El servicio se reinicia en bucle**: `journalctl -u obsidian-mcp -n 200`;
  típicamente `ConfigError` (variable mal escrita) o el vault sin configurar en `ob`.
