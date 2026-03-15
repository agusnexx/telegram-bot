FROM python:3.11-slim

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Instalar Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Directorio de trabajo
WORKDIR /app

# Instalar PyTorch CPU-only (mucho más liviano que la versión con CUDA)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Copiar e instalar el resto de dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Playwright con Chromium
RUN playwright install chromium --with-deps

# Copiar el código
COPY . .

CMD ["python3", "bot.py"]
