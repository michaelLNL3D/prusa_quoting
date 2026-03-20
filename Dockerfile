# ── Stage 1: download & extract slicers ──────────────────────────────────────
FROM ubuntu:22.04 AS slicer-extract

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y wget && rm -rf /var/lib/apt/lists/*

# PrusaSlicer AppImage → extract to /opt/prusaslicer
ARG PRUSASLICER_VERSION=2.8.1
ARG PRUSASLICER_BUILD=202409181354
RUN wget -q \
    "https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSASLICER_VERSION}/PrusaSlicer-${PRUSASLICER_VERSION}+linux-x64-GTK3-${PRUSASLICER_BUILD}.AppImage" \
    -O /tmp/prusaslicer.AppImage \
  && chmod +x /tmp/prusaslicer.AppImage \
  && mkdir -p /opt \
  && cd /opt \
  && /tmp/prusaslicer.AppImage --appimage-extract \
  && mv squashfs-root prusaslicer \
  && rm /tmp/prusaslicer.AppImage

# OrcaSlicer AppImage → extract to /opt/orcaslicer
ARG ORCASLICER_VERSION=2.2.0
RUN wget -q \
    "https://github.com/SoftFever/OrcaSlicer/releases/download/v${ORCASLICER_VERSION}/OrcaSlicer_Linux_V${ORCASLICER_VERSION}.AppImage" \
    -O /tmp/orcaslicer.AppImage \
  && chmod +x /tmp/orcaslicer.AppImage \
  && cd /opt \
  && /tmp/orcaslicer.AppImage --appimage-extract \
  && mv squashfs-root orcaslicer \
  && rm /tmp/orcaslicer.AppImage


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    # Python
    python3 python3-pip \
    # GTK / graphics libs needed by PrusaSlicer/OrcaSlicer
    libgtk-3-0 libglib2.0-0 libdbus-1-3 \
    libgl1 libglu1-mesa \
    libxrender1 libxrandr2 libxss1 libxcursor1 libxcomposite1 \
    libxi6 libxtst6 libfontconfig1 libfreetype6 \
    libatk1.0-0 libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
    # Virtual framebuffer (PrusaSlicer may probe for display even in CLI mode)
    xvfb \
    # Misc
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Copy extracted slicers from stage 1
COPY --from=slicer-extract /opt/prusaslicer /opt/prusaslicer
COPY --from=slicer-extract /opt/orcaslicer  /opt/orcaslicer

# Thin wrapper scripts so callers don't need to worry about LD_LIBRARY_PATH
RUN printf '#!/bin/sh\nexport LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\nexec /opt/prusaslicer/usr/bin/prusa-slicer "$@"\n' \
      > /usr/local/bin/prusa-slicer && chmod +x /usr/local/bin/prusa-slicer

RUN printf '#!/bin/sh\nexport LD_LIBRARY_PATH="/opt/orcaslicer/usr/lib:/opt/orcaslicer/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n# binary may be orca-slicer or orca_slicer depending on build\nBIN=$(ls /opt/orcaslicer/usr/bin/orca* 2>/dev/null | head -1)\n[ -z "$BIN" ] && BIN=$(ls /opt/orcaslicer/orca* 2>/dev/null | head -1)\nexec "$BIN" "$@"\n' \
      > /usr/local/bin/orca-slicer && chmod +x /usr/local/bin/orca-slicer

# App
WORKDIR /app
COPY app.py quote.py ./

# /data is a volume used for:
#   /data/prusaslicer_config  →  user's PrusaSlicer config (printer/filament/print profiles)
#   /data/exports             →  scratch GCode/3MF exports
#   /data/presets.json        →  saved app presets
RUN mkdir -p /data/prusaslicer_config /data/exports

ENV PRUSASLICER_PATH=/usr/local/bin/prusa-slicer
ENV ORCASLICER_PATH=/usr/local/bin/orca-slicer
ENV PRUSA_VENDOR_DIR=/opt/prusaslicer/usr/share/PrusaSlicer/profiles
ENV PRUSA_USER_DIR=/data/prusaslicer_config
ENV PRESETS_FILE=/data/presets.json
ENV PORT=5111
# PrusaSlicer reads HOME for its own config search path
ENV HOME=/root

VOLUME ["/data"]
EXPOSE 5111

# Run under xvfb so any display probe doesn't crash
CMD ["xvfb-run", "--auto-servernum", "python3", "app.py"]
