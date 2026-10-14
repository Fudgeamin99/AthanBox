# Azan SIP Box as a container: the scheduler and control panel, without the box's own screen.
# Linux hosts only, with host networking (multicast paging and SIP) - see compose.yaml.
FROM python:3.12-slim

# fail2ban, nftables and iptables are for the optional "security" container (see compose.yaml),
# which runs from this same image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata fail2ban nftables iptables \
 && rm -f /etc/fail2ban/jail.d/defaults-debian.conf \
 && rm -rf /var/lib/apt/lists/*

# The program lives in /app. Settings, recordings, the password and the log live in /data,
# a volume, so they survive pulling a new image.
RUN useradd --uid 1000 --home-dir /data --no-create-home --shell /usr/sbin/nologin azan \
 && mkdir -p /data/audio && chown -R azan:azan /data
WORKDIR /app
COPY azan_sip ./azan_sip
COPY BOX_CHANGELOG.md ./
COPY packaging/docker/entrypoint.sh /usr/local/bin/azan-sip-entrypoint
COPY packaging/box/security-helper.py /usr/local/lib/azan-sip/security-helper.py

# AZAN_DOCKER: Settings > Updates explains how to pull a new image instead of updating in place.
ENV AZAN_DOCKER=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# where the security container leaves its status, for the control panel to show
ENV AZAN_SECURITY_STATUS=/data/security/status.json
# No USER line: the entrypoint starts as root only to give /data to user azan (1000), then
# runs Azan SIP as that user.
VOLUME /data
EXPOSE 8765
# Python runs as process 1, which ignores SIGTERM: stop with Ctrl+C's signal, so the Box signs out
# of the phone system and stops in a moment instead of being killed after Docker's timeout.
STOPSIGNAL SIGINT
ENTRYPOINT ["/usr/local/bin/azan-sip-entrypoint"]
