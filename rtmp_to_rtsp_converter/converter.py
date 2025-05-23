import subprocess
import logging
import time
import os
import signal
import sys

# Настройка логирования
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)]) # Явное указание вывода в stdout

FFMPEG_PATH = "ffmpeg" # Предполагается, что ffmpeg находится в PATH, иначе укажите полный путь

class RTMPToRTSPConverter:
    def __init__(self, rtmp_url, rtsp_port, rtsp_path="live"):
        self.rtmp_url = rtmp_url
        self.rtsp_port = rtsp_port
        self.rtsp_path = rtsp_path
        self.process = None
        self.output_rtsp_url = f"rtsp://127.0.0.1:{self.rtsp_port}/{self.rtsp_path}"

    def start(self):
        if self.process and self.process.poll() is None:
            logging.warning(f"Конвертер для {self.rtmp_url} уже запущен.")
            return

        # Команда для конвертации RTMP в RTSP с использованием ffmpeg.
        # Этот скрипт предполагает, что ОТДЕЛЬНЫЙ RTSP-сервер (например, mediamtx) уже запущен
        # и слушает на 127.0.0.1:{self.rtsp_port}.
        # FFmpeg будет получать RTMP поток и ПЕРЕНАПРАВЛЯТЬ его на этот RTSP-сервер.

        # URL, на который FFmpeg будет отправлять RTSP поток
        self.output_rtsp_url_for_ffmpeg_push = f"rtsp://127.0.0.1:{self.rtsp_port}/{self.rtsp_path}"
        # RTSP-сервер (например, mediamtx) должен быть настроен так, чтобы принимать потоки по этому пути.
        # Например, если ffmpeg отправляет на rtsp://127.0.0.1:8554/stream1,
        # mediamtx сделает поток доступным по адресу rtsp://<ip_mediamtx_сервера>:8554/stream1.

        cmd_ffmpeg_push = [
            FFMPEG_PATH,
            '-loglevel', 'info',   # Уровень логирования ffmpeg (info для отладки)
            '-i', self.rtmp_url,   # Входной RTMP URL
            '-c:v', 'copy',        # Копировать видеокодек (без перекодирования)
            '-c:a', 'copy',        # Копировать аудиокодек (можно заменить на 'aac' при проблемах)
            # '-bsf:a', 'aac_adtstoasc', # Используется, если аудио перекодируется в AAC
            '-f', 'rtsp',          # Формат выходного потока - RTSP
            '-rtsp_transport', 'tcp', # Использовать TCP для RTSP (более надежно)
            self.output_rtsp_url_for_ffmpeg_push # URL для отправки RTSP потока
        ]

        logging.info(f"Запуск конвертера для {self.rtmp_url} -> {self.output_rtsp_url_for_ffmpeg_push}")
        logging.info(f"Команда FFmpeg: {' '.join(cmd_ffmpeg_push)}")

        try:
            # Запуск процесса ffmpeg
            self.process = subprocess.Popen(cmd_ffmpeg_push, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logging.info(f"Процесс FFmpeg запущен с PID: {self.process.pid} для {self.rtmp_url}")
            logging.info(f"RTSP поток должен быть доступен (через RTSP-сервер) по адресу: rtsp://<ip_rtsp_сервера>:{self.rtsp_port}/{self.rtsp_path}")
            # В реальном сценарии здесь можно было бы также запускать mediamtx, если он не запущен.
        except FileNotFoundError:
            logging.error(f"FFmpeg не найден по пути {FFMPEG_PATH}. Убедитесь, что FFmpeg установлен и добавлен в PATH.")
            self.process = None
        except Exception as e:
            logging.error(f"Не удалось запустить FFmpeg для {self.rtmp_url}: {e}")
            self.process = None

    def stop(self):
        if self.process and self.process.poll() is None:
            logging.info(f"Остановка конвертера для {self.rtmp_url} (PID: {self.process.pid})")
            # Отправка SIGINT (Ctrl+C) для корректного завершения ffmpeg
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=10) # Ожидание завершения ffmpeg
                logging.info(f"Процесс FFmpeg {self.process.pid} корректно завершен.")
            except subprocess.TimeoutExpired:
                logging.warning(f"Процесс FFmpeg {self.process.pid} не завершился корректно, принудительное завершение.")
                self.process.kill() # Принудительное завершение, если не остановился
                self.process.wait()
                logging.info(f"Процесс FFmpeg {self.process.pid} принудительно завершен.")
            self.process = None
        else:
            logging.info(f"Конвертер для {self.rtmp_url} не запущен.")

    def get_status(self):
        if self.process and self.process.poll() is None:
            return "запущен"
        # Проверка stderr на наличие ошибок ffmpeg, если процесс завершился
        if self.process:
            # stdout, stderr = self.process.communicate() # Следует использовать осторожно, если процесс еще жив
            # Для завершенного процесса poll() не None.
            # Если poll() не None, значит процесс завершился.
            # Код возврата можно проверить через self.process.returncode.
            if self.process.returncode != 0 and self.process.returncode is not None:
                # Попытка прочитать stderr, если доступно
                try:
                    # Если communicate() уже был вызван, stderr здесь может быть пустым.
                    # Лучше обрабатывать stderr/stdout в отдельном потоке для мониторинга в реальном времени.
                    # Для простоты просто пометим как завершенный с ошибкой.
                    # err_output = self.process.stderr.read().decode('utf-8', errors='ignore') if self.process.stderr else "Нет stderr"
                    # logging.error(f"Процесс FFmpeg для {self.rtmp_url} завершился с кодом ошибки {self.process.returncode}. Stderr: {err_output[:500]}")
                    logging.error(f"Процесс FFmpeg для {self.rtmp_url} завершился с кодом ошибки {self.process.returncode}.")
                except Exception as e:
                    logging.error(f"Ошибка чтения stderr для завершенного процесса {self.rtmp_url}: {e}")
                return f"завершен_с_ошибкой (код {self.process.returncode})"
            return "остановлен"
        return "ожидание" # Не запущен или остановлен

# Глобальный словарь для хранения экземпляров конвертеров (для нескольких потоков)
converters = {}

def start_conversion(stream_id, rtmp_url, rtsp_port, rtsp_path="live"):
    if stream_id in converters and converters[stream_id].get_status() == "запущен":
        logging.warning(f"Поток {stream_id} уже конвертируется.")
        return converters[stream_id]

    converter = RTMPToRTSPConverter(rtmp_url, rtsp_port, rtsp_path)
    converters[stream_id] = converter
    converter.start()
    return converter

def stop_conversion(stream_id):
    if stream_id in converters:
        converters[stream_id].stop()
        # Опционально удалить из словаря: del converters[stream_id]
        # но сохранение позволяет проверять статус или перезапускать
    else:
        logging.warning(f"Активная конвертация для ID потока не найдена: {stream_id}")

def get_all_statuses():
    return {sid: conv.get_status() for sid, conv in converters.items()}

def handle_exit():
    logging.info("Завершение работы всех конвертеров...")
    for stream_id in list(converters.keys()): # list() для избежания проблем, если stop_conversion изменяет словарь
        stop_conversion(stream_id)
    logging.info("Все конвертеры остановлены. Выход.")

def main_cli():
    # Это простой CLI для тестирования.
    # Более надежное приложение может использовать файл конфигурации или веб-интерфейс.
    print("Конвертер RTMP в RTSP (CLI)")
    print("---------------------------------")
    print("Требуется отдельно запущенный RTSP-сервер (например, mediamtx).")
    print("FFmpeg будет получать RTMP и отправлять на rtsp://127.0.0.1:<rtsp_port>/<rtsp_path>")
    print("Итоговый RTSP поток будет доступен с RTSP-сервера по этому адресу.")
    print("---------------------------------")

    # Регистрация обработчиков сигналов для корректного завершения
    signal.signal(signal.SIGINT, lambda s, f: (logging.info("Получен SIGINT, завершение работы..."), handle_exit(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda s, f: (logging.info("Получен SIGTERM, завершение работы..."), handle_exit(), sys.exit(0)))

    stream_counter = 0
    try:
        while True:
            print("\nОпции:")
            print("1. Начать новую конвертацию")
            print("2. Остановить конвертацию")
            print("3. Посмотреть статусы")
            print("4. Выход")
            choice = input("Введите выбор: ")

            if choice == '1':
                rtmp_url = input("Введите RTMP URL источника (например, rtmp://server/live/stream_key): ")
                rtsp_port = input("Введите порт RTSP-сервера (например, 8554 для mediamtx по умолчанию): ")
                rtsp_path_segment = input("Введите сегмент пути RTSP для этого потока (например, mystream1): ")
                if not rtmp_url or not rtsp_port.isdigit() or not rtsp_path_segment:
                    print("Неверный ввод. Пожалуйста, попробуйте снова.")
                    continue
                
                stream_id = f"stream_{stream_counter}"
                stream_counter += 1
                
                start_conversion(stream_id, rtmp_url, int(rtsp_port), rtsp_path_segment)
                if converters[stream_id].process:
                     print(f"Конвертация запущена для {stream_id}. Выходной поток (через RTSP-сервер): rtsp://<ip_rtsp_сервера>:{rtsp_port}/{rtsp_path_segment}")
                else:
                    print(f"Не удалось запустить конвертацию для {stream_id}.")


            elif choice == '2':
                stream_id_to_stop = input("Введите ID потока для остановки (например, stream_0): ")
                if stream_id_to_stop in converters:
                    stop_conversion(stream_id_to_stop)
                    print(f"Конвертация остановлена для {stream_id_to_stop}.")
                else:
                    print(f"ID потока {stream_id_to_stop} не найден.")

            elif choice == '3':
                statuses = get_all_statuses()
                if not statuses:
                    print("Нет активных или прошлых конвертаций.")
                else:
                    print("\nСтатусы конвертаций:")
                    for sid, status in statuses.items():
                        converter = converters[sid]
                        print(f"  ID: {sid}, RTMP: {converter.rtmp_url}, RTSP Push: {converter.output_rtsp_url_for_ffmpeg_push}, Статус: {status}")
            
            elif choice == '4':
                break
            else:
                print("Неверный выбор. Пожалуйста, попробуйте снова.")
    
    except KeyboardInterrupt:
        logging.info("Получено прерывание с клавиатуры, завершение работы...")
    finally:
        handle_exit()
        sys.exit(0)

if __name__ == "__main__":
    # Пример использования:
    # Перед запуском убедитесь, что RTSP-сервер, такой как mediamtx, запущен.
    # Например, запустите mediamtx: ./mediamtx
    # Обычно он слушает порт 8554.

    # Для запуска CLI:
    main_cli()

    # Программный пример (закомментируйте main_cli() для использования):
    # rtmp_source = "rtmp://your_rtmp_server/app/stream_key" # Замените вашим RTMP источником
    # rtsp_server_port = 8554 # По умолчанию для mediamtx
    # rtsp_stream_path = "mystream"

    # converter1 = start_conversion("stream1", rtmp_source, rtsp_server_port, rtsp_stream_path)

    # try:
    #     while True:
    #         time.sleep(5)
    #         status = converter1.get_status()
    #         logging.info(f"Статус потока stream1 ({converter1.rtmp_url}): {status}")
    #         if status != "запущен" and status != "ожидание": # если завершен или ошибка
    #             logging.info("Процесс конвертера, похоже, остановился. Завершение основного цикла.")
    #             break
    #         # Здесь можно добавить дополнительную логику, например, перезапуск при сбое
    # except KeyboardInterrupt:
    #     logging.info("Получено прерывание с клавиатуры. Остановка конвертера.")
    # finally:
    #     stop_conversion("stream1")
    #     logging.info("Скрипт конвертера завершил работу.")
