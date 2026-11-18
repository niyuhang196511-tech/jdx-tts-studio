type PeakRequest = { id: number; samples: Float32Array; duration: number };
type PeakResponse = { id: number; data: Float32Array; duration: number };
const worker = globalThis as unknown as {
  onmessage: ((event: MessageEvent<PeakRequest>) => void) | null;
  postMessage: (value: PeakResponse, transfer: Transferable[]) => void;
};

worker.onmessage = ({ data: request }) => {
  const count = Math.min(6000, Math.max(100, Math.ceil(request.duration * 100)));
  const data = new Float32Array(count);
  const step = request.samples.length / count;
  for (let index = 0; index < count; index++) {
    let peak = 0;
    for (let sample = Math.floor(index * step); sample < Math.min(request.samples.length, Math.ceil((index + 1) * step)); sample++) {
      peak = Math.max(peak, Math.abs(request.samples[sample]));
    }
    data[index] = peak;
  }
  worker.postMessage({ id: request.id, data, duration: request.duration }, [data.buffer]);
};

export {};
