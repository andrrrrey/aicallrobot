FROM python:3.11-slim

WORKDIR /app

# --- Системные пакеты + сборка pjproject (pjsua2) ---
# pjsua2 нет в PyPI — собираем pjproject с python-биндингами (SWIG).
# --disable-sound: контейнер без звуковой карты (используем null-устройство и
# кастомный AudioMediaPort). --disable-video: видео не нужно.
ARG PJPROJECT_VERSION=2.14.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ make build-essential \
        libffi-dev libssl-dev uuid-dev \
        swig wget ca-certificates \
        curl ffmpeg \
    && wget -q "https://github.com/pjsip/pjproject/archive/refs/tags/${PJPROJECT_VERSION}.tar.gz" -O /tmp/pjproject.tar.gz \
    && tar -xzf /tmp/pjproject.tar.gz -C /tmp \
    && cd "/tmp/pjproject-${PJPROJECT_VERSION}" \
    && ./configure CFLAGS="-fPIC -O2" --enable-shared --disable-video --disable-sound \
    && make dep && make && make install && ldconfig \
    && cd "/tmp/pjproject-${PJPROJECT_VERSION}/pjsip-apps/src/swig/python" \
    && make && make install \
    && cd /app \
    && python -c "import pjsua2; print('pjsua2 OK')" \
    && rm -rf /tmp/pjproject* /var/lib/apt/lists/*

ENV LD_LIBRARY_PATH=/usr/local/lib

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/logs /app/recordings /app/scenarios

EXPOSE 8000 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
