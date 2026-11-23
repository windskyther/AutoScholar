// Edge-refraction technique informed by shuding/liquid-glass (MIT).
// Attribution and license: ../public/THIRD_PARTY_NOTICES.txt. This is a Web approximation.
export interface RefractionMap {
  url: string;
  width: number;
  height: number;
  scale: number;
}

export function createRefractionMap(width: number, height: number, radius: number): RefractionMap | null {
  if (![width, height, radius].every(Number.isFinite) || width < 1 || height < 1) return null;
  const canvas = document.createElement('canvas');
  canvas.width = Math.min(640, Math.ceil(width));
  canvas.height = Math.min(128, Math.ceil(height));
  const context = canvas.getContext('2d');
  if (!context) return null;
  const image = context.createImageData(canvas.width, canvas.height);
  const halfWidth = width / 2;
  const halfHeight = height / 2;
  const corner = Math.max(0, Math.min(radius, halfWidth, halfHeight));
  const band = Math.min(18, halfHeight);
  const strength = 7;

  for (let row = 0; row < canvas.height; row++) {
    for (let column = 0; column < canvas.width; column++) {
      const x = (column + 0.5) / canvas.width * width - halfWidth;
      const y = (row + 0.5) / canvas.height * height - halfHeight;
      const qx = Math.abs(x) - halfWidth + corner;
      const qy = Math.abs(y) - halfHeight + corner;
      const px = Math.max(qx, 0);
      const py = Math.max(qy, 0);
      const length = Math.hypot(px, py);
      const distance = length + Math.min(Math.max(qx, qy), 0) - corner;
      const proximity = Math.max(0, Math.min(1, 1 + distance / band));
      const bend = proximity * proximity * (3 - 2 * proximity) * strength;
      const nx = length > 0 ? px / length * Math.sign(x) : qx > qy ? Math.sign(x) : 0;
      const ny = length > 0 ? py / length * Math.sign(y) : qy >= qx ? Math.sign(y) : 0;
      const index = (row * canvas.width + column) * 4;
      image.data[index] = Math.round((0.5 - nx * bend / (strength * 2)) * 255);
      image.data[index + 1] = Math.round((0.5 - ny * bend / (strength * 2)) * 255);
      image.data[index + 2] = 0;
      image.data[index + 3] = 255;
    }
  }
  context.putImageData(image, 0, 0);
  return { url: canvas.toDataURL('image/png'), width, height, scale: strength * 2 };
}
