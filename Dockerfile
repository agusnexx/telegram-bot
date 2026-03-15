FROM python:3.11-slim

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    wget \
    git \
    nodejs \
    npm \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Instalar Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Directorio de trabajo
WORKDIR /app

# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Playwright con Chromium
RUN playwright install chromium
RUN playwright install-deps chromium

# Copiar el código
COPY . .

# Descargar modelo Whisper medium al buildear
RUN python3 -c "import whisper; whisper.load_model('medium')"

CMD ["python3", "bot.py"]
