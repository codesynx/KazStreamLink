import subprocess
import logging
import time
import os
import signal
import sys
import threading
import re # Для парсинга логов FFmpeg
import psutil # Для CPU/Memory usage
from collections import deque # Для хранения последних значений метрик

# Настройка логирования
# Убедимся, что логирование настроено один раз
if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])

FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg") # Можно переопределить через переменную окружения

class RTMPToRTSPConverter:
    def __init__(self, stream_id, rtmp_url, rtsp_server_host, rtsp_port, rtsp_path="live"): # Добавлен stream_id
        self.stream_id = stream_id
        self.rtmp_url = rtmp_url
        self.rtsp_server_host = rtsp_server_host
        self.rtsp_port = rtsp_port
        self.rtsp_path = rtsp_path
        self.process = None
        self.ffmpeg_logs = deque(maxlen=100) # Хранение последних 100 логов ffmpeg
        self.last_error_message = None
        self.status = "ожидание"
        self.metrics = {
            "bitrate_kbit": "N/A",
            "fps": "N/A",
            "cpu_percent": "N/A",
            "memory_mb": "N/A",
            "dropped_frames": 0, # Счетчик предполагаемых дропов/ошибок
            "last_update_time": None
        }
        self.metrics_history = deque(maxlen=60) # Хранить историю метрик за ~минуту (если обновлять каждую секунду)
        self._metrics_thread = None
        self._metrics_thread_stop_event = threading.Event()


        # URL, на который FFmpeg будет отправлять RTSP поток
        self.output_rtsp_url_for_ffmpeg_push = f"rtsp://{self.rtsp_server_host}:{self.rtsp_port}/{self.rtsp_path}"
        
        # URL для клиента
        self.final_rtsp_url_for_client = f"rtsp://{self.rtsp_server_host}:{self.rtsp_port}/{self.rtsp_path}"

    def _parse_ffmpeg_progress_output(self, line):
        """Парсит строку из stdout FFmpeg (от -progress) для извлечения метрик."""
        # Пример вывода -progress:
        # frame=1
        # fps=0.00
        # stream_0_0_q=-1.0
        # bitrate=  -1.0kbits/s
        # total_size=1337
        # out_time_us=66666
        # out_time_ms=66666
        # out_time=00:00:00.066666
        # dup_frames=0
        # drop_frames=0
        # speed=2.63x
        # progress=continue
        parts = line.split('=')
        if len(parts) == 2:
            key = parts[0].strip()
            value = parts[1].strip()
            
            if key == "bitrate":
                if "kbits/s" in value:
                    val_num_str = value.replace("kbits/s", "").strip()
                    try:
                        self.metrics["bitrate_kbit"] = round(float(val_num_str), 2)
                    except ValueError:
                        self.metrics["bitrate_kbit"] = "N/A (parse)"
                else: # Иногда может быть просто число в bits/s
                    try:
                        self.metrics["bitrate_kbit"] = round(float(value) / 1000, 2) # Переводим в kbit/s
                    except ValueError:
                         pass # Игнорируем, если не число
            elif key == "fps":
                try:
                    self.metrics["fps"] = round(float(value), 2)
                except ValueError:
                    self.metrics["fps"] = "N/A (parse)"
            elif key == "drop_frames": # FFmpeg сам считает дропнутые кадры
                try:
                    # Это будет общее количество дропнутых кадров, а не инкремент
                    self.metrics["dropped_frames"] = int(value)
                except ValueError:
                    pass # Игнорируем

    def _log_stream_output(self, pipe, log_type):
        """Читает вывод из потока (stdout/stderr) FFmpeg, логирует и парсит метрики."""
        try:
            for line_bytes in iter(pipe.readline, b''):
                if line_bytes:
                    line = line_bytes.decode('utf-8', errors='replace').strip()
                    
                    if log_type == "stdout_progress": # Парсим вывод -progress из stdout
                        self._parse_ffmpeg_progress_output(line)
                        # Можно не логировать каждую строку прогресса, чтобы не засорять основной лог
                        # logging.debug(f"[FFmpeg {self.stream_id} PROGRESS]: {line}") 
                    elif log_type == "stderr_errors": # Логируем ошибки и общий вывод из stderr
                        log_entry = f"[FFmpeg {self.stream_id} STDERR]: {line}"
                        logging.error(log_entry) # Логируем все из stderr как error для внимания
                        self.ffmpeg_logs.append(log_entry) # Добавляем в отображаемые логи только stderr
                        if "error" in line.lower() or "failed" in line.lower() or "corrupt" in line.lower() or "unable" in line.lower():
                            self.last_error_message = line
                            # Можно увеличить счетчик dropped_frames и здесь, если ошибка связана с данными
                            # self.metrics["dropped_frames"] = self.metrics.get("dropped_frames", 0) + 1
                    else: # Неожиданный log_type
                        log_entry = f"[FFmpeg {self.stream_id} {log_type.upper()}]: {line}"
                        logging.info(log_entry)
                        self.ffmpeg_logs.append(log_entry)
                
                # Проверяем, не завершился ли процесс или не установлен ли флаг остановки потока метрик
                if (self.process and self.process.poll() is not None) or self._metrics_thread_stop_event.is_set():
                    break
        except Exception as e:
            error_msg = f"Ошибка чтения вывода FFmpeg для {self.stream_id}: {e}"
            logging.error(error_msg)
            self.ffmpeg_logs.append(error_msg) # Добавляем ошибку чтения в лог ffmpeg
            if not self.last_error_message: self.last_error_message = error_msg
        finally:
            pipe.close()
            if self.process and self.process.poll() is not None and self.status not in ["остановлен", "завершен_с_ошибкой", "ошибка_запуска"]:
                self._update_status_after_process_exit()

    def _monitor_system_metrics(self):
        """Периодически собирает CPU и Memory usage для процесса FFmpeg."""
        try:
            ffmpeg_psutil_process = psutil.Process(self.process.pid)
            while not self._metrics_thread_stop_event.is_set() and ffmpeg_psutil_process.is_running():
                try:
                    self.metrics["cpu_percent"] = ffmpeg_psutil_process.cpu_percent(interval=1.0) # интервал для расчета CPU
                    self.metrics["memory_mb"] = round(ffmpeg_psutil_process.memory_info().rss / (1024 * 1024), 2) # RSS в МБ
                    self.metrics["last_update_time"] = time.time()
                    self.metrics_history.append(self.metrics.copy()) # Сохраняем копию текущих метрик
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    logging.warning(f"Не удалось получить метрики для процесса FFmpeg {self.process.pid} (возможно, он завершился).")
                    break
                except Exception as e:
                    logging.error(f"Ошибка при сборе системных метрик для {self.stream_id}: {e}")
                
                # Пауза перед следующим сбором метрик, но прерываемая событием остановки
                # Это предотвращает зависание потока надолго, если его нужно остановить
                self._metrics_thread_stop_event.wait(timeout=1.0) # Ожидание 1 секунду или до события
            
            logging.info(f"Поток мониторинга системных метрик для {self.stream_id} завершен.")

        except psutil.NoSuchProcess:
            logging.info(f"Процесс FFmpeg {self.process.pid} для {self.stream_id} не найден для мониторинга (возможно, уже завершился).")
        except Exception as e:
            logging.error(f"Критическая ошибка в потоке мониторинга системных метрик для {self.stream_id}: {e}")
        finally:
            # Сбрасываем CPU/Memory, если поток мониторинга завершился
            self.metrics["cpu_percent"] = "N/A"
            self.metrics["memory_mb"] = "N/A"


    def _update_status_after_process_exit(self):
        # Эта функция вызывается, когда self.process.poll() is not None
        if self.process and self.process.returncode is not None: # Убедимся, что returncode есть
            return_code = self.process.returncode
            if self.status == "останавливается": # Если остановка была инициирована нами
                self.status = "остановлен"
                logging.info(f"Процесс FFmpeg для {self.stream_id} остановлен (код: {return_code}).")
            elif return_code == 0 and self.status != "ошибка_запуска": # Успешное завершение, не связанное с ошибкой запуска
                self.status = "остановлен" # Или "завершен_успешно"
                logging.info(f"Процесс FFmpeg для {self.stream_id} завершился успешно (код: 0).")
            elif self.status != "ошибка_запуска": # Завершение с ошибкой, но не ошибка при самом запуске
                error_msg = f"Процесс FFmpeg для {self.stream_id} завершился с кодом ошибки {return_code}."
                logging.error(error_msg)
                if not self.last_error_message:
                    self.last_error_message = error_msg
                self.status = f"завершен_с_ошибкой (код {return_code})"
            # Если self.status == "ошибка_запуска", он уже установлен и не меняется здесь
        else: # Процесс None или returncode is None (не должно быть здесь, если poll() не None)
            if self.status not in ["ошибка_запуска", "ожидание"]: # Если не ошибка запуска и не начальное состояние
                 self.status = "неизвестно" # Неожиданное состояние


    def start(self):
        if self.status in ["запущен", "запускается"]:
            logging.warning(f"Конвертер для {self.stream_id} уже запущен или запускается.")
            return

        self.status = "запускается"
        self.ffmpeg_logs.clear()
        self.metrics_history.clear()
        self.last_error_message = None
        self.metrics = {
            "bitrate_kbit": "N/A", "fps": "N/A", "cpu_percent": "N/A",
            "memory_mb": "N/A", "dropped_frames": 0, "last_update_time": None
        }
        self._metrics_thread_stop_event.clear() # Сбрасываем событие остановки

        # Используем -progress pipe:1 для структурированного вывода статистики в stdout.
        # stderr по-прежнему будет использоваться для логов ошибок и общего вывода FFmpeg.
        cmd_ffmpeg_push = [
            FFMPEG_PATH,
            # Опции для потенциального снижения задержки и улучшения стабильности:
            '-analyzeduration', '1000000', # Анализировать вход не более 1 сек
            '-probesize', '1000000',       # Читать не более 1МБ для определения формата
            '-fflags', 'nobuffer',         # Уменьшить входную буферизацию (риск потерь при плохой сети)
            '-fflags', '+discardcorrupt',  # Пытаться отбрасывать поврежденные пакеты (добавляем к существующим fflags)
            
            # Опции для автоматического переподключения к источнику RTMP
            '-reconnect', '1',
            '-reconnect_at_eof', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '4', # Максимальная задержка перед попыткой переподключения

            '-loglevel', 'error',          # Оставляем только ошибки в stderr, основная инфа через -progress
            '-progress', 'pipe:1',         # Направляем вывод прогресса в stdout (pipe:1)
            '-nostdin',                    # Важно при использовании pipe:1

            '-i', self.rtmp_url,           # Входной поток

            # Копирование кодеков - наиболее оптимально по CPU/Memory и вносит мин. задержку
            '-c:v', 'copy',
            '-c:a', 'copy',
            
            '-f', 'rtsp',                  # Формат вывода
            '-rtsp_transport', 'tcp',      # Транспорт для RTSP (TCP более надежен)
            self.output_rtsp_url_for_ffmpeg_push # URL назначения
        ]
        # Примечание: опция -fflags может быть указана несколько раз, или значения могут быть объединены через '+'.
        # Например, '-fflags', 'nobuffer+discardcorrupt'
        # Для ясности здесь разделено, но FFmpeg может потребовать объединения.
        # Проверим, как FFmpeg обработает несколько -fflags. Если будут проблемы, объединим.
        # Корректный способ указать несколько fflags - это '+имяфлага', если он добавляется к предыдущим,
        # или просто перечислить их через запятую для некоторых флагов, но не для fflags.
        # Безопаснее всего: '-fflags', 'nobuffer discardcorrupt' (если так поддерживается) или использовать '+'
        # Исправим на более надежный вариант с '+':
        # Удалим предыдущие -fflags и добавим один объединенный:
        cmd_ffmpeg_push = [
            FFMPEG_PATH,
            '-analyzeduration', '1000000',
            '-probesize', '1000000',
            '-fflags', 'nobuffer+discardcorrupt', # Объединенные флаги
            # Опции переподключения временно убраны из-за проблем совместимости
            # '-reconnect', '1',
            # '-reconnect_streamed', '1',
            # '-reconnect_delay_max', '4000',
            '-loglevel', 'error',
            '-progress', 'pipe:1',
            '-nostdin',
            '-i', self.rtmp_url,
            '-c:v', 'copy',
            '-c:a', 'copy',
            '-f', 'rtsp',
            '-rtsp_transport', 'tcp',
            self.output_rtsp_url_for_ffmpeg_push
        ]

        logging.info(f"Запуск конвертера для {self.stream_id} ({self.rtmp_url} -> {self.output_rtsp_url_for_ffmpeg_push})")
        logging.info(f"Оптимизированная команда FFmpeg для {self.stream_id}: {' '.join(cmd_ffmpeg_push)}")

        try:
            self.process = subprocess.Popen(
                cmd_ffmpeg_push,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Добавляем close_fds=True для Linux/macOS для лучшего управления файловыми дескрипторами
                # и startupinfo для Windows, чтобы не создавалось окно консоли для ffmpeg
                close_fds=(os.name == 'posix') 
                # startupinfo=startupinfo # Для Windows, если нужно скрыть окно
            )
            self.status = "запущен"
            logging.info(f"Процесс FFmpeg для {self.stream_id} запущен с PID: {self.process.pid}")

            # Запускаем потоки для чтения stdout (для -progress) и stderr (для ошибок) FFmpeg
            stdout_pipe = self.process.stdout
            stderr_pipe = self.process.stderr

            self.stdout_thread = threading.Thread(
                target=self._log_stream_output, args=(stdout_pipe, "stdout_progress"), daemon=True, name=f"ffmpeg_stdout_{self.stream_id}"
            )
            self.stderr_thread = threading.Thread(
                target=self._log_stream_output, args=(stderr_pipe, "stderr_errors"), daemon=True, name=f"ffmpeg_stderr_{self.stream_id}"
            )
            self.stdout_thread.start()
            self.stderr_thread.start()

            # Запускаем поток для мониторинга системных метрик
            self._metrics_thread = threading.Thread(
                target=self._monitor_system_metrics, daemon=True, name=f"system_metrics_{self.stream_id}"
            )
            self._metrics_thread.start()

        except FileNotFoundError:
            error_msg = f"FFmpeg не найден по пути {FFMPEG_PATH}. Убедитесь, что FFmpeg установлен и добавлен в PATH (или доступен в Docker контейнере)."
            logging.error(error_msg)
            self.last_error_message = error_msg
            self.status = "ошибка_запуска"
            if self.process: self.process = None # Убедимся, что процесс None
        except Exception as e:
            error_msg = f"Не удалось запустить FFmpeg для {self.stream_id}: {e}"
            logging.error(error_msg)
            self.last_error_message = error_msg
            self.status = "ошибка_запуска"
            if self.process: self.process = None # Убедимся, что процесс None

    def stop(self):
        logging.info(f"Запрос на остановку конвертера для {self.stream_id} (текущий статус: {self.status})")
        self._metrics_thread_stop_event.set() # Сигнализируем потоку метрик о необходимости завершения

        if self.process and self.process.poll() is None: # Если процесс существует и еще запущен
            if self.status not in ["останавливается", "остановлен", "завершен_с_ошибкой", "ошибка_запуска"]:
                self.status = "останавливается"
                logging.info(f"Отправка SIGINT процессу FFmpeg {self.process.pid} для {self.stream_id}...")
                try:
                    self.process.send_signal(signal.SIGINT)
                    # Не ждем здесь долго, так как _log_stream_output и _update_status_after_process_exit обработают завершение
                    # self.process.wait(timeout=10) # Это может заблокировать Streamlit UI
                except Exception as e:
                    logging.error(f"Ошибка при отправке SIGINT процессу {self.process.pid}: {e}")
                    # Если отправка сигнала не удалась, возможно, стоит попробовать kill
                    if self.process.poll() is None:
                        logging.warning(f"SIGINT не удался, попытка SIGKILL для процесса {self.process.pid}")
                        self.process.kill()
            else:
                 logging.info(f"Процесс FFmpeg для {self.stream_id} уже останавливается или остановлен.")
        else:
            logging.info(f"Процесс FFmpeg для {self.stream_id} не запущен или уже завершен.")
            if self.status not in ["ошибка_запуска", "завершен_с_ошибкой"]:
                 self.status = "остановлен" # Если не было ошибок, но процесс не найден

        # Ожидание завершения потоков логов и метрик (с таймаутом)
        if hasattr(self, 'stderr_thread') and self.stderr_thread.is_alive():
            self.stderr_thread.join(timeout=2.0)
        if self._metrics_thread and self._metrics_thread.is_alive():
            self._metrics_thread.join(timeout=2.0)
        
        # Финальное обновление статуса, если процесс завершился
        if self.process and self.process.poll() is not None:
            self._update_status_after_process_exit()
        elif not self.process and self.status not in ["ошибка_запуска", "ожидание"]:
            self.status = "остановлен"


    def get_status(self):
        """Возвращает текущий статус конвертера, обновляя его, если процесс завершился."""
        if self.process and self.process.poll() is not None: # Процесс завершился
            if self.status not in ["остановлен", "завершен_с_ошибкой", "ошибка_запуска"]: # Если статус еще не финальный
                self._update_status_after_process_exit()
        elif self.process and self.process.poll() is None: # Процесс еще жив
             if self.status == "запускается": # Если все еще запускается, но процесс уже есть
                 self.status = "запущен"
        # Если self.process is None, то статус должен быть "ожидание" или "ошибка_запуска"
        return self.status
    
    def get_ffmpeg_logs(self):
        """Возвращает последние логи FFmpeg."""
        return list(self.ffmpeg_logs) # Возвращаем копию

    def get_last_error(self):
        """Возвращает последнее сообщение об ошибке."""
        return self.last_error_message

    def get_metrics(self):
        """Возвращает текущие собранные метрики."""
        # Обновляем CPU/Memory если процесс еще жив, но _monitor_system_metrics не успел
        # Это не очень хорошо, лучше чтобы _monitor_system_metrics сам обновлял
        # if self.process and self.process.poll() is None and self.metrics["cpu_percent"] == "N/A":
        #     try:
        #         p = psutil.Process(self.process.pid)
        #         self.metrics["cpu_percent"] = p.cpu_percent(interval=0.1)
        #         self.metrics["memory_mb"] = round(p.memory_info().rss / (1024 * 1024), 2)
        #     except (psutil.NoSuchProcess, psutil.AccessDenied):
        #         pass # Ошибки здесь игнорируем, метрики останутся N/A
        return self.metrics.copy()

    def get_metrics_history(self):
        return list(self.metrics_history)

# Глобальный словарь для хранения экземпляров конвертеров (для нескольких потоков)
# Этот словарь будет управляться Streamlit через st.session_state
# converters_store = {} # Переименуем, чтобы не конфликтовать с возможным импортом

# Функции для управления конвертерами (будут использоваться Streamlit)
def create_and_start_conversion(stream_id, rtmp_url, rtsp_server_host, rtsp_port, rtsp_path="live"):
    """Создает, запускает и возвращает экземпляр конвертера."""
    logging.info(f"Запрос на создание и запуск конверсии для ID: {stream_id}")
    converter = RTMPToRTSPConverter(stream_id, rtmp_url, rtsp_server_host, rtsp_port, rtsp_path)
    converter.start()
    return converter

def stop_specific_conversion(converter_instance):
    """Останавливает конкретный экземпляр конвертера."""
    if converter_instance:
        logging.info(f"Запрос на остановку конверсии для ID: {converter_instance.stream_id}")
        converter_instance.stop()

# Функции handle_exit и main_cli больше не нужны, так как управление будет через Streamlit.
# def handle_exit():
#     ...
# def main_cli():
#     ...

# if __name__ == "__main__":
    # main_cli() # CLI больше не используется
    # Пример программного использования (для отладки, если нужно)
    # pass
