"""Made-up music for the fingerprint tests: a melody, chords, a bass line and a beat per seed (deterministic)."""
import math, random, wave, array
def song(seed, path, secs=90, sr=22050):
    rnd = random.Random(seed)
    base = 110 * 2 ** (rnd.randint(0, 11) / 12)
    beat = 60 / rnd.randint(90, 140) / 2
    # a melody that never repeats exactly (real music doesn't either; a loop matches everywhere equally well)
    notes = [rnd.choice([0,2,3,5,7,8,10,12,14,15]) for _ in range(int(secs / beat) + 2)]
    buf = array.array('h')
    for i in range(int(secs * sr)):
        t = i / sr
        k = int(t / beat)
        n = notes[k]
        f = base * 2 ** (n / 12)
        env = math.exp(-((t % beat) / beat) * 3)
        v = env * (0.5 * math.sin(2 * math.pi * f * t) + 0.25 * math.sin(2 * math.pi * f * 2.01 * t) + 0.15 * math.sin(2 * math.pi * f * 1.5 * t))
        v += 0.2 * math.sin(2 * math.pi * base / 2 * t) * (1 if (k // 4) % 2 else 0.5)
        if k % 4 == 0 and (t % beat) < 0.03: v += rnd.uniform(-0.6, 0.6)
        buf.append(int(max(-1, min(1, v * 0.6)) * 32767))
    w = wave.open(path, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(buf.tobytes()); w.close()


if __name__ == "__main__":
    for s in range(1, 7):
        song(s, f"song{s}.wav")
