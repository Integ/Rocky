/** Decorative orbital core. State changes affect emphasis, never fabricated telemetry. */
export function startVisuals({ canvas, getStatus = () => "demo" }) {
  const context = canvas?.getContext("2d", { alpha: true });
  if (!context) return () => {};

  const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
  let reduced = motionPreference.matches;
  let disposed = false;
  let width = 1;
  let height = 1;
  let ratio = 1;
  let frame = 0;
  let statusTimer = 0;
  let previousTime = 0;
  let elapsed = 0;
  let lastStatus = "";
  const amber = "255, 148, 60";
  const pale = "255, 222, 184";
  const points = Array.from({ length: 340 }, (_, index) => {
    const y = 1 - (2 * (index + 0.5)) / 340;
    const distance = Math.sqrt(1 - y * y);
    const angle = index * Math.PI * (3 - Math.sqrt(5));
    return [Math.cos(angle) * distance, y, Math.sin(angle) * distance];
  });

  function status() {
    try {
      return getStatus() || "offline";
    } catch {
      return "offline";
    }
  }

  function render() {
    if (disposed || document.hidden || width < 2 || height < 2) return;
    const state = status();
    lastStatus = state;
    const intensity = state === "offline" ? 0.42 : state === "waiting" ? 0.65 : 1;
    const time = reduced ? 11 : elapsed;
    const radius = Math.min(width, height) * 0.247;
    const cx = width / 2;
    const cy = height * 0.505;
    const rotation = time * 0.055 + 0.55;
    const tilt = -0.22;
    const cosR = Math.cos(rotation);
    const sinR = Math.sin(rotation);
    const cosT = Math.cos(tilt);
    const sinT = Math.sin(tilt);

    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    context.globalAlpha = intensity;

    const halo = context.createRadialGradient(cx, cy, 0, cx, cy, radius * 2.1);
    halo.addColorStop(0, `rgba(${amber}, 0.08)`);
    halo.addColorStop(0.38, `rgba(${amber}, 0.05)`);
    halo.addColorStop(0.72, `rgba(${amber}, 0.018)`);
    halo.addColorStop(1, `rgba(${amber}, 0)`);
    context.fillStyle = halo;
    context.fillRect(0, 0, width, height);

    function project([x, y, z]) {
      const xx = x * cosR + z * sinR;
      const zz = -x * sinR + z * cosR;
      const yy = y * cosT - zz * sinT;
      const depth = y * sinT + zz * cosT;
      const perspective = 3.8 / (3.8 - depth);
      return { x: cx + xx * radius * perspective, y: cy + yy * radius * perspective, depth };
    }

    function orbit(rx, ry, angle, offset, foreground = false) {
      context.save();
      context.translate(cx, cy);
      context.rotate(angle);
      context.lineWidth = 0.7;
      context.strokeStyle = `rgba(${amber}, ${foreground ? 0.24 : 0.15})`;
      context.beginPath();
      context.ellipse(0, 0, rx, ry, 0, foreground ? 0 : 0, foreground ? Math.PI : Math.PI * 2);
      context.stroke();
      const orbitPhase = time * 0.075 + offset;
      context.strokeStyle = `rgba(${pale}, 0.52)`;
      context.lineWidth = 1;
      context.beginPath();
      context.ellipse(0, 0, rx, ry, 0, orbitPhase, orbitPhase + 0.14);
      context.stroke();
      const px = Math.cos(orbitPhase) * rx;
      const py = Math.sin(orbitPhase) * ry;
      context.fillStyle = `rgba(${pale}, 0.9)`;
      context.shadowColor = `rgba(${amber}, 0.9)`;
      context.shadowBlur = 9;
      context.beginPath();
      context.arc(px, py, 1.7, 0, Math.PI * 2);
      context.fill();
      context.restore();
    }

    // The outer instrumentation is purely illustrative geometry.
    context.save();
    context.translate(cx, cy);
    context.strokeStyle = `rgba(${amber}, 0.1)`;
    context.lineWidth = 0.6;
    context.setLineDash([1, 8]);
    context.beginPath();
    context.arc(0, 0, radius * 1.86, 0, Math.PI * 2);
    context.stroke();
    context.setLineDash([]);
    for (let index = 0; index < 72; index++) {
      const angle = (index / 72) * Math.PI * 2;
      const long = index % 6 === 0;
      const inner = radius * 1.94;
      const outer = inner + (long ? 7 : 2.5);
      context.strokeStyle = `rgba(${amber}, ${long ? 0.23 : 0.1})`;
      context.beginPath();
      context.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner);
      context.lineTo(Math.cos(angle) * outer, Math.sin(angle) * outer);
      context.stroke();
    }
    for (const offset of [0, Math.PI]) {
      context.strokeStyle = `rgba(${amber}, 0.36)`;
      context.lineWidth = 1;
      context.beginPath();
      const start = -time * 0.018 + offset - 0.4;
      context.arc(0, 0, radius * 1.73, start, start + 0.35);
      context.stroke();
    }
    context.restore();

    orbit(radius * 1.72, radius * 0.56, -0.48, 1.1);
    orbit(radius * 1.71, radius * 0.49, 0.5, 3.2);
    orbit(radius * 1.43, radius * 0.55, -1.08, 5.3);

    // Soft backplate makes the wireframe read as a single suspended volume.
    const volume = context.createRadialGradient(
      cx - radius * 0.3, cy - radius * 0.3, 0, cx, cy, radius * 1.04,
    );
    volume.addColorStop(0, "rgba(31, 24, 18, 0.5)");
    volume.addColorStop(0.78, "rgba(12, 14, 15, 0.5)");
    volume.addColorStop(1, `rgba(${amber}, 0.02)`);
    context.fillStyle = volume;
    context.beginPath();
    context.arc(cx, cy, radius * 1.03, 0, Math.PI * 2);
    context.fill();

    context.lineWidth = 0.65;
    for (let latitude = -3; latitude <= 3; latitude++) {
      const phi = (latitude / 4) * (Math.PI / 2);
      context.beginPath();
      for (let index = 0; index <= 96; index++) {
        const theta = (index / 96) * Math.PI * 2;
        const p = project([Math.cos(phi) * Math.cos(theta), Math.sin(phi), Math.cos(phi) * Math.sin(theta)]);
        if (index === 0) context.moveTo(p.x, p.y);
        else context.lineTo(p.x, p.y);
      }
      context.strokeStyle = `rgba(${amber}, ${latitude === 0 ? 0.22 : 0.12})`;
      context.stroke();
    }
    for (let longitude = 0; longitude < 12; longitude++) {
      const theta = (longitude / 12) * Math.PI * 2;
      context.beginPath();
      for (let index = 0; index <= 64; index++) {
        const phi = (index / 64) * Math.PI * 2;
        const p = project([Math.cos(phi) * Math.cos(theta), Math.sin(phi), Math.cos(phi) * Math.sin(theta)]);
        if (index === 0) context.moveTo(p.x, p.y);
        else context.lineTo(p.x, p.y);
      }
      context.strokeStyle = `rgba(${amber}, 0.13)`;
      context.stroke();
    }

    const projected = points.map(project).sort((a, b) => a.depth - b.depth);
    for (const p of projected) {
      const prominence = (p.depth + 1) / 2;
      context.fillStyle = `rgba(${prominence > 0.83 ? pale : amber}, ${0.12 + prominence * 0.68})`;
      context.beginPath();
      context.arc(p.x, p.y, 0.45 + prominence * 0.75, 0, Math.PI * 2);
      context.fill();
    }

    context.strokeStyle = `rgba(${amber}, 0.4)`;
    context.lineWidth = 0.7;
    context.beginPath();
    context.arc(cx, cy, radius * 1.03, 0, Math.PI * 2);
    context.stroke();
    orbit(radius * 1.72, radius * 0.56, -0.48, 1.1, true);

    // Tiny axis marks anchor the core without inventing measurements.
    context.strokeStyle = `rgba(${pale}, 0.32)`;
    context.lineWidth = 0.7;
    for (const direction of [-1, 1]) {
      const x = cx + direction * radius * 2.13;
      context.beginPath();
      context.moveTo(x - 3, cy);
      context.lineTo(x + 3, cy);
      context.moveTo(x, cy - 3);
      context.lineTo(x, cy + 3);
      context.stroke();
    }
    context.globalAlpha = 1;
  }

  function animate(timestamp) {
    frame = 0;
    if (disposed || reduced || document.hidden) return;
    if (previousTime) elapsed += Math.min((timestamp - previousTime) / 1000, 0.1);
    previousTime = timestamp;
    render();
    frame = window.requestAnimationFrame(animate);
  }

  function restart() {
    if (frame) window.cancelAnimationFrame(frame);
    frame = 0;
    if (statusTimer) window.clearInterval(statusTimer);
    statusTimer = 0;
    previousTime = 0;
    if (disposed || document.hidden) return;
    render();
    if (!reduced) frame = window.requestAnimationFrame(animate);
    else {
      statusTimer = window.setInterval(() => {
        if (status() !== lastStatus) render();
      }, 1000);
    }
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    width = Math.max(1, rect.width);
    height = Math.max(1, rect.height);
    ratio = Math.max(1, Math.min(window.devicePixelRatio || 1, 2));
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    render();
  }

  function onMotionChange(event) {
    reduced = event.matches;
    restart();
  }

  const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(resize);
  observer?.observe(canvas);
  window.addEventListener("resize", resize, { passive: true });
  document.addEventListener("visibilitychange", restart);
  motionPreference.addEventListener("change", onMotionChange);
  resize();
  restart();

  return () => {
    disposed = true;
    if (frame) window.cancelAnimationFrame(frame);
    if (statusTimer) window.clearInterval(statusTimer);
    observer?.disconnect();
    window.removeEventListener("resize", resize);
    document.removeEventListener("visibilitychange", restart);
    motionPreference.removeEventListener("change", onMotionChange);
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, canvas.width, canvas.height);
  };
}
