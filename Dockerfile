# Используем официальный образ Python
FROM python:3.9-slim

# Устанавливаем рабочую директорию в контейнере
WORKDIR /app

# Устанавливаем FFmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Устанавливаем Streamlit, psutil и pandas
# Для простоты установим напрямую. В более крупных проектах лучше использовать requirements.txt
RUN pip install streamlit psutil pandas

# Копируем директорию rtmp_to_rtsp_converter в контейнер
COPY rtmp_to_rtsp_converter/ rtmp_to_rtsp_converter/

# Копируем Streamlit приложение
COPY streamlit_app.py .

# Указываем порт, который Streamlit использует по умолчанию
EXPOSE 8501

# Указываем команду для запуска Streamlit приложения
# streamlit run streamlit_app.py --server.port=8501 --server.address=0.0.0.0
# --server.headless=true рекомендуется для Docker, чтобы не открывать браузер
CMD ["streamlit", "run", "streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
