import streamlit as st
import uuid
import time
import pandas as pd # Добавлено для графиков
from rtmp_to_rtsp_converter.converter import create_and_start_conversion, stop_specific_conversion, RTMPToRTSPConverter
import logging
import sys # Добавлено для logging.StreamHandler

# Настройка логирования (если еще не настроено из converter.py)
if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])

# Инициализация session_state для хранения конвертеров
if 'converters' not in st.session_state:
    st.session_state.converters = {} # Словарь {stream_id: converter_instance}

if 'next_stream_id_counter' not in st.session_state:
    st.session_state.next_stream_id_counter = 0

st.set_page_config(page_title="KazStreamLink: RTMP-RTSP Конвертер", layout="wide")

st.title("KazStreamLink: RTMP → RTSP Конвертер")
st.markdown("""
Добро пожаловать в KazStreamLink! Этот инструмент позволяет конвертировать RTMP видеопотоки в RTSP.
Для работы **требуется запущенный RTSP-сервер** (например, mediamtx), на который будут перенаправляться потоки.
При использовании Docker, RTSP-сервер (mediamtx) запускается автоматически.
""")

# --- Форма для добавления нового потока ---
st.header("Добавить новую конвертацию")
with st.form("new_stream_form", clear_on_submit=True):
    rtmp_url_input = st.text_input("RTMP URL источника:", placeholder="rtmp://your-source-server/live/stream_key")
    
    # Подсказки для Docker
    # Подсказки для Docker
    st.caption("При использовании Docker с `docker-compose.yml` из этого проекта, используйте 'mediamtx' как хост RTSP-сервера. Для локального запуска (без Docker) обычно '127.0.0.1'.")
    col1, col2, col3 = st.columns(3)
    with col1:
        rtsp_server_host_input = st.text_input("Хост RTSP-сервера (для FFmpeg):", value="127.0.0.1", help="Для Docker: 'mediamtx'. Локально: '127.0.0.1'.")
    with col2:
        rtsp_port_input = st.number_input("Порт RTSP-сервера (для FFmpeg):", value=8554, min_value=1, max_value=65535, help="Порт, на котором слушает ваш RTSP-сервер (mediamtx).")
    with col3:
        rtsp_path_input = st.text_input("Путь RTSP-потока (на сервере):", placeholder="mystream", help="Например, 'mystream1'. Итоговый URL будет rtsp://<хост_docker>:<порт_mediamtx>/mystream1")

    submitted = st.form_submit_button("Начать конвертацию")

    if submitted:
        if not rtmp_url_input:
            st.error("RTMP URL не может быть пустым.")
        elif not rtsp_server_host_input:
            st.error("Хост RTSP-сервера не может быть пустым.")
        elif not rtsp_path_input:
            st.error("Путь RTSP-потока не может быть пустым.")
        else:
            stream_id = f"stream_{st.session_state.next_stream_id_counter}"
            st.session_state.next_stream_id_counter += 1
            
            st.info(f"Запуск конвертации для {stream_id} ({rtmp_url_input})...")
            try:
                converter_instance = create_and_start_conversion(
                    stream_id,
                    rtmp_url_input,
                    rtsp_server_host_input,
                    int(rtsp_port_input),
                    rtsp_path_input
                )
                st.session_state.converters[stream_id] = converter_instance
                st.success(f"Конвертация {stream_id} запущена!")
                # Небольшая задержка, чтобы дать FFmpeg время запуститься перед обновлением UI
                time.sleep(1) 
                st.rerun()
            except Exception as e:
                st.error(f"Ошибка при запуске конвертации {stream_id}: {e}")
                logging.error(f"Ошибка UI при запуске {stream_id}: {e}", exc_info=True)


# --- Контейнер для отображения потоков (для real-time обновления) ---
streams_placeholder = st.empty()

def display_streams():
    with streams_placeholder.container():
        st.header("Активные и завершенные конвертации")

        if not st.session_state.converters:
            st.info("Нет активных конвертаций.")
            return # Выходим, если нет конвертеров

        active_converters_ids = list(st.session_state.converters.keys())

        for stream_id in active_converters_ids:
            if stream_id not in st.session_state.converters:
                continue
                
            converter = st.session_state.converters[stream_id]
            status = converter.get_status()
            metrics = converter.get_metrics()

            with st.expander(f"Поток: {stream_id} | Статус: {status.upper()}", expanded=True):
                col_info, col_action = st.columns([3,1])
                
                with col_info:
                    st.markdown(f"**RTMP Источник:** `{converter.rtmp_url}`")
                    st.markdown(f"**FFmpeg отправляет на:** `{converter.output_rtsp_url_for_ffmpeg_push}`")
                    
                    client_rtsp_url_display = converter.final_rtsp_url_for_client
                    if converter.rtsp_server_host == "mediamtx":
                        client_rtsp_url_display = f"rtsp://<IP_хоста_Docker>:{converter.rtsp_port}/{converter.rtsp_path}"
                        st.markdown(f"**Примерный RTSP URL для клиента:** `{client_rtsp_url_display}` (замените `<IP_хоста_Docker>`)")
                    else:
                         st.markdown(f"**RTSP URL для клиента:** `{client_rtsp_url_display}`")

                    st.markdown("---")
                    st.subheader("Метрики потока:")
                    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
                    with m_col1:
                        st.metric(label="Битрейт (kbit/s)", value=str(metrics.get("bitrate_kbit", "N/A")))
                    with m_col2:
                        st.metric(label="FPS", value=str(metrics.get("fps", "N/A")))
                    with m_col3:
                        st.metric(label="CPU FFmpeg (%)", value=str(metrics.get("cpu_percent", "N/A")))
                    with m_col4:
                        st.metric(label="Память FFmpeg (MB)", value=str(metrics.get("memory_mb", "N/A")))
                    
                    st.metric(label="Ошибки/Дропы (счетчик)", value=str(metrics.get("dropped_frames", 0)))
                    
                    if metrics.get("last_update_time"):
                        st.caption(f"Метрики обновлены: {time.strftime('%H:%M:%S', time.localtime(metrics['last_update_time']))}")

                    if status == "завершен_с_ошибкой" or status == "ошибка_запуска":
                        last_error = converter.get_last_error()
                        if last_error:
                            st.error(f"Последняя ошибка FFmpeg: {last_error}")
                
                with col_action:
                    # Кнопки должны быть уникальными, даже если форма перерисовывается
                    # Используем stream_id в key кнопки
                    if status in ["запущен", "запускается"]:
                        if st.button("Остановить", key=f"stop_{stream_id}_{time.time()}"): # Добавляем time для уникальности при быстром обновлении
                            st.info(f"Остановка потока {stream_id}...")
                            stop_specific_conversion(converter)
                            time.sleep(0.5) # Короткая пауза перед rerun
                            st.rerun()
                    elif status not in ["останавливается"]:
                        if st.button("Удалить из списка", key=f"remove_{stream_id}_{time.time()}"):
                            del st.session_state.converters[stream_id]
                            st.rerun()
                
                ffmpeg_logs = converter.get_ffmpeg_logs()
                if ffmpeg_logs:
                    with st.container():
                        st.markdown("**Логи FFmpeg (последние):**")
                        log_text = "\n".join(ffmpeg_logs)
                        st.code(log_text, language="log", line_numbers=False)
                
                st.markdown("---")
                metrics_history = converter.get_metrics_history()
                if metrics_history:
                    df_metrics = pd.DataFrame(metrics_history)
                    charts_to_display = {}
                    if 'bitrate_kbit' in df_metrics.columns and pd.to_numeric(df_metrics['bitrate_kbit'], errors='coerce').notnull().any():
                        charts_to_display['Битрейт (kbit/s)'] = pd.to_numeric(df_metrics['bitrate_kbit'], errors='coerce')
                    if 'cpu_percent' in df_metrics.columns and pd.to_numeric(df_metrics['cpu_percent'], errors='coerce').notnull().any():
                        charts_to_display['CPU FFmpeg (%)'] = pd.to_numeric(df_metrics['cpu_percent'], errors='coerce')
                    if 'fps' in df_metrics.columns and pd.to_numeric(df_metrics['fps'], errors='coerce').notnull().any():
                         charts_to_display['FPS'] = pd.to_numeric(df_metrics['fps'], errors='coerce')

                    if charts_to_display:
                        st.subheader("История метрик:")
                        df_chart = pd.DataFrame(charts_to_display)
                        df_chart.dropna(inplace=True)
                        if not df_chart.empty:
                            st.line_chart(df_chart)
                        else:
                            st.caption("Недостаточно данных для построения графика истории.")
                    else:
                        st.caption("Нет данных для графика истории метрик.")

# --- Боковая панель ---
st.sidebar.header("О проекте")
st.sidebar.info(
    "KazStreamLink - это инструмент для конвертации RTMP в RTSP, "
    "использующий FFmpeg для обработки видео и Streamlit для веб-интерфейса. "
    "Для раздачи RTSP потоков рекомендуется использовать mediamtx."
)
st.sidebar.markdown("---")
# Убираем кнопку "Обновить статусы", так как будет автообновление
# if st.sidebar.button("Обновить статусы всех потоков"):
#    st.rerun()

# --- Логика автообновления ---
# Этот цикл будет выполняться постоянно, обновляя содержимое streams_placeholder
# Важно: st.rerun() внутри этого цикла не нужен, так как мы напрямую обновляем placeholder.
# Однако, чтобы кнопки работали корректно и Streamlit обрабатывал их состояние,
# st.rerun() все еще нужен ПОСЛЕ нажатия кнопки.
# Для простого периодического обновления данных без взаимодействия, можно было бы обойтись без rerun,
# но кнопки усложняют ситуацию.
#
# Простой способ заставить Streamlit обновляться - это использовать st.rerun() в конце скрипта
# после небольшой задержки. Это будет вызывать полный пересчет скрипта.

# Первоначальное отображение
display_streams()

# Цикл для периодического обновления (если не было взаимодействия, которое вызвало rerun)
# Этот подход может быть не идеальным, так как st.rerun() перезапускает весь скрипт.
# Более продвинутые техники могут использовать st.experimental_fragment или кастомные компоненты.
# Для простоты, оставим st.rerun() после действий пользователя и кнопку ручного обновления.
# Если нужно "живое" обновление без действий, то это сложнее.

# Для имитации "живого" обновления, можно добавить кнопку "Обновить сейчас" в основной части
# или положиться на то, что пользователь будет взаимодействовать с UI (нажимать кнопки),
# что и так вызывает st.rerun().

# Если мы хотим принудительное автообновление каждые N секунд:
# Это должно быть в самом конце скрипта.
# time.sleep(2) # Интервал обновления в секундах
# st.rerun()
# ВНИМАНИЕ: Бесконечный st.rerun() с time.sleep() может привести к проблемам с производительностью
# и поведением виджетов, особенно если интервал слишком мал.
# Streamlit не предназначен для такого типа постоянного фонового обновления без взаимодействия.
# Лучше всего, если обновления происходят в ответ на действия пользователя или через
# более контролируемые механизмы, если они появятся в Streamlit.

# Пока оставим обновление по кнопкам. Если нужно более "живое", можно исследовать
# st.experimental_fragment или другие подходы.
# Для текущей задачи, обновление при взаимодействии (нажатие "Остановить", "Удалить", "Начать")
# и кнопка "Обновить статусы всех потоков" должны быть достаточны.
if st.sidebar.button("Обновить отображение потоков"):
    st.rerun()
