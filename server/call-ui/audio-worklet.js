// Mono 8 kHz / 16-bit transport. Bounded jitter buffer and simple resampling.
class CallAudio extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ring = new Float32Array(4096); this.read = 0; this.write = 0; this.available = 0;
    this.playPhase = 0; this.current = 0; this.next = 0; this.started = false;
    this.micPhase = 0; this.sum = 0; this.samples = 0; this.packet = new Int16Array(160); this.used = 0;
    this.meterAt = 0; this.peak = 0;
    this.port.onmessage = ({data}) => {
      if (!(data instanceof ArrayBuffer) || data.byteLength !== 320) return;
      const input = new DataView(data);
      // Discard old audio once latency exceeds 160 ms.
      if (this.available > 1280) { this.read = this.write; this.available = 0; this.started = false; }
      for (let i = 0; i < 160; i++) {
        this.ring[this.write] = input.getInt16(i * 2, true) / 32768;
        this.write = (this.write + 1) % this.ring.length; this.available++;
      }
    };
  }
  take() {
    if (!this.available) return 0;
    const sample = this.ring[this.read]; this.read = (this.read + 1) % this.ring.length; this.available--;
    return sample;
  }
  process(inputs, outputs) {
    const mic = inputs[0]?.[0]; const output = outputs[0]?.[0];
    if (!output) return true;
    for (let i = 0; i < output.length; i++) {
      const sample = mic ? mic[i] : 0;
      this.peak = Math.max(this.peak, Math.abs(sample)); this.sum += sample; this.samples++;
      this.micPhase += 8000;
      if (this.micPhase >= sampleRate) {
        this.micPhase -= sampleRate;
        this.packet[this.used++] = Math.round(Math.max(-1, Math.min(1, this.sum / this.samples)) * 32767);
        this.sum = 0; this.samples = 0;
        if (this.used === 160) {
          const buffer = this.packet.buffer; this.port.postMessage(buffer, [buffer]);
          this.packet = new Int16Array(160); this.used = 0;
        }
      }
      if (!this.started && this.available >= 480) {
        this.current = this.take(); this.next = this.take(); this.playPhase = 0; this.started = true;
      }
      output[i] = this.started ? this.current + (this.next - this.current) * this.playPhase : 0;
      this.playPhase += 8000 / sampleRate;
      if (this.playPhase >= 1) {
        this.playPhase -= 1; this.current = this.next; this.next = this.take();
        if (!this.available && this.current === 0 && this.next === 0) this.started = false;
      }
    }
    this.meterAt += output.length;
    if (this.meterAt >= sampleRate / 10) {
      this.port.postMessage({peak:this.peak}); this.meterAt = 0; this.peak = 0;
    }
    return true;
  }
}
registerProcessor('call-audio', CallAudio);
