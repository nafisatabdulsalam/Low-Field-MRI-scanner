import csv
import serial
import time
import sys
import math
from pathlib import Path

# ─── USER SETTINGS ────────────────────────────────────────────────────────────
INPUT_CSV = "path.csv"
OUTPUT_CSV = "data.csv"
CMD_DELAY = 0.25
DWELL_S = 5          # Dwell time after each move (for stability)
PAUSE_AFTER_READ = 2.0  # Short pause after reading before next move
FIRST_PAUSE_S = 5.0     # Extra pause after FIRST coordinate only
ARDUINO_BAUD = 9600
SENSOR_PORT = "COM4"

# Sensor communication protocol constants
FRAME_LEN = 13
INIT_CMD_1 = b"\x14" * 6
INIT_CMD_2 = b"\xff" * 6
INIT_CMD_3 = b"\x01" * 6
START_STREAM_CMD = b"\x04" * 6
ACK_CMD = b"\x03" * 6


def init_sensor(port: serial.Serial) -> None:
    """Full wake-up sequence for the ALPHALAB sensor."""
    print("[INFO] Initialising Gauss Meter probe...")
    port.baudrate, port.bytesize = 1200, serial.SEVENBITS
    port.dtr = port.rts = True
    time.sleep(0.1)
    port.baudrate, port.bytesize = 115200, serial.EIGHTBITS
    port.rts = False
    for cmd in (INIT_CMD_1, INIT_CMD_2, INIT_CMD_3):
        port.write(cmd)
    time.sleep(0.5)
    port.reset_input_buffer()
    port.write(START_STREAM_CMD)
    print("[INFO] Gauss Meter probe ready.")


# ─── Ports ────────────────────────────────────────────────────────────────────
ARDUINO_PORT = (
    Path("arduino_port.txt").read_text().strip()
    if Path("arduino_port.txt").exists()
    else sys.exit("arduino_port.txt missing")
)

try:
    ser_motion = serial.Serial(ARDUINO_PORT, ARDUINO_BAUD, timeout=1)
    time.sleep(2)
    print(f"[INFO] Motion link on {ARDUINO_PORT}")

    ser_sensor = serial.Serial(SENSOR_PORT, timeout=2)
    init_sensor(ser_sensor)

except serial.SerialException as e:
    sys.exit(f"Serial error: {e}")


# ─── Motion helpers ───────────────────────────────────────────────────────────
def step_axis(axis, delta):
    if delta == 0:
        return
    cmd = (axis + ('+\n' if delta > 0 else '-\n')).encode()
    for _ in range(abs(delta)):
        ser_motion.write(cmd)
        time.sleep(CMD_DELAY)


def goto(target, current):
    dx, dy, dz = (t - c for t, c in zip(target, current))
    step_axis('X', dx)
    step_axis('Y', dy)
    step_axis('Z', dz)
    print(f"[MOVE] Moved to {target}, pausing for {DWELL_S}s to stabilize...")
    time.sleep(DWELL_S)
    return target


# ─── Sensor read with Consistency Check ──────────────────────────────
def read_sensor() -> float:
    """Reads a data frame, checks magnitude consistency, and determines the sign."""
    ser_sensor.reset_input_buffer()

    consistent_readings = []
    retry_count = 0
    max_retries = 5

    while len(consistent_readings) < 3 and retry_count < max_retries:
        frame = ser_sensor.read(FRAME_LEN)
        if len(frame) == FRAME_LEN:
            ser_sensor.write(ACK_CMD)
            time.sleep(0.1)

            magnitude = int.from_bytes(frame[-2:], "big", signed=False) / 100.0
            sign_byte = frame[8]

            actual_magnitude = -magnitude if sign_byte == 0x0a else magnitude if sign_byte in [0x0b, 0x02] else None

            if sign_byte not in [0x0a, 0x0b, 0x02]:
                print(f"[WARN] Ambiguous sign detection. Raw bytes: {frame.hex(' ')}")

            if actual_magnitude is not None:
                consistent_readings.append(actual_magnitude)
                if len(consistent_readings) > 1 and not math.isclose(
                        consistent_readings[-1], consistent_readings[-2], rel_tol=1e-2):
                    consistent_readings.pop(0)
            else:
                retry_count += 1
        else:
            retry_count += 1
            init_sensor(ser_sensor)

    if consistent_readings:
        return consistent_readings[-1]

    print("[ERROR] Unable to get valid reading after multiple attempts.")
    return float('nan')


# ─── Main Program ─────────────────────────────────────────────────────────────
def main():
    path = []
    try:
        with open(INPUT_CSV, newline='') as f:
            csv_reader = csv.reader(f, delimiter=' ')
            next(csv_reader, None)
            for r in csv_reader:
                if not r or any(not c.strip() for c in r):
                    continue
                path.append(tuple(map(int, r[:3])))
    except FileNotFoundError:
        sys.exit(f"[ERROR] Input file not found: {INPUT_CSV}")
    except Exception as e:
        sys.exit(f"[ERROR] Could not process {INPUT_CSV}. Error: {e}")

    if not path:
        sys.exit("Path is empty or could not be read!")
    print(f"[INFO] {len(path)} points loaded from {INPUT_CSV}")

    with open(OUTPUT_CSV, 'w', newline='') as fout:
        wr = csv.writer(fout, delimiter=' ')
        wr.writerow(["x", "y", "z", "reading"])
        current = path[0]  # start at first coordinate
        print("\n[INFO] Starting automated scan...")

        # Move to initial position before loop
        print(f"[MOVE] Moving to first coordinate {current}...")
        current = goto(current, (0, 0, 0))
        print(f"[INFO] Pausing {FIRST_PAUSE_S}s before first reading...")
        time.sleep(FIRST_PAUSE_S)

        for i, tgt in enumerate(path, 1):
            # Move only if not first coordinate
            if i != 1:
                current = goto(tgt, current)

            # Pause for stability before reading
            print(f"[INFO] Pausing {DWELL_S}s before reading at {tgt}...")
            time.sleep(DWELL_S)

            val = read_sensor()
            if not math.isnan(val):
                wr.writerow([*tgt, f"{val:.2f}"])
                fout.flush()
                print(f"[DATA] {i}/{len(path)} | Position: {tgt} | Reading: {val:.2f} G")
            else:
                print(f"[WARN] Skipped {tgt} (NaN reading)")

            # Pause briefly before next move
            print(f"[INFO] Short pause {PAUSE_AFTER_READ}s before next move...")
            time.sleep(PAUSE_AFTER_READ)

    ser_motion.close()
    ser_sensor.close()
    print(f"\n[SUCCESS] Finished! All data saved in {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
