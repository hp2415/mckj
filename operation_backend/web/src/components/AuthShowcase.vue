<template>
  <aside class="auth-stage">
    <img class="auth-bg" src="/auth-bg.png" alt="" />
    <div class="auth-veil" />

    <div class="brand-mark">
      <img class="op-logo" src="/favicon.ico" alt="米宝" />
      <span>米宝运营</span>
    </div>

    <div class="scene" aria-hidden="true">
      <div class="ring ring-lg" />
      <div class="ring ring-md" />
      <div class="ring ring-sm" />

      <div class="glass glass-a" />
      <div class="glass glass-b" />

      <div class="planet">
        <i class="planet-core" />
        <i class="planet-shine" />
      </div>

      <div class="orb orb-a" />
      <div class="orb orb-b" />
      <div class="spark s1" />
      <div class="spark s2" />
      <div class="spark s3" />
    </div>
  </aside>
</template>

<style scoped>
.auth-stage {
  position: absolute;
  inset: 0;
  overflow: hidden;
  background: #07111f;
}

.auth-bg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center;
  pointer-events: none;
}

.auth-veil {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(105deg, rgba(6, 16, 32, 0.18) 0%, rgba(6, 16, 32, 0.08) 46%, rgba(7, 18, 36, 0.42) 100%);
  pointer-events: none;
}

.brand-mark {
  position: absolute;
  z-index: 2;
  top: 28px;
  left: 32px;
  display: flex;
  align-items: center;
  gap: 0.7rem;
  color: #fff;
  font-weight: 650;
  letter-spacing: 0.02em;
  text-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}

.brand-mark .op-logo {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.28);
}

.scene {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: min(62%, 860px);
  display: grid;
  place-items: center;
  perspective: 900px;
  transform-style: preserve-3d;
  pointer-events: none;
}

.ring {
  position: absolute;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 50%;
  transform: rotateX(66deg) rotateZ(-16deg);
}

.ring-lg {
  width: min(70%, 420px);
  aspect-ratio: 1;
  animation: orbit 28s linear infinite;
}

.ring-md {
  width: min(52%, 310px);
  aspect-ratio: 1;
  border-color: rgba(255, 255, 255, 0.24);
  animation: orbit 20s linear infinite reverse;
}

.ring-sm {
  width: min(32%, 190px);
  aspect-ratio: 1;
  border-color: rgba(var(--op-brand-rgb), 0.5);
  animation: orbit 14s linear infinite;
}

.planet {
  position: relative;
  width: 132px;
  height: 132px;
  z-index: 1;
  animation: floaty 8s ease-in-out infinite;
}

.planet-core {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background:
    radial-gradient(circle at 32% 28%, #dbeafe 0%, #60a5fa 22%, var(--op-brand) 52%, #0b3a7a 100%);
  box-shadow:
    0 30px 60px rgba(7, 17, 31, 0.28),
    inset -18px -22px 40px rgba(7, 24, 48, 0.32);
}

.planet-shine {
  position: absolute;
  width: 42px;
  height: 24px;
  left: 26px;
  top: 24px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.45);
  filter: blur(4px);
}

.glass {
  position: absolute;
  border-radius: 22px;
  background: linear-gradient(145deg, rgba(255, 255, 255, 0.14), rgba(255, 255, 255, 0.04));
  border: 1px solid rgba(255, 255, 255, 0.16);
  backdrop-filter: blur(14px);
  box-shadow: 0 18px 40px rgba(3, 12, 28, 0.18);
}

.glass-a {
  width: 150px;
  height: 96px;
  left: 14%;
  top: 22%;
  transform: rotate(-12deg);
  animation: floaty 7.5s ease-in-out infinite;
}

.glass-b {
  width: 118px;
  height: 80px;
  right: 18%;
  bottom: 24%;
  transform: rotate(10deg);
  animation: floaty 9s ease-in-out infinite reverse;
}

.orb {
  position: absolute;
  border-radius: 50%;
  background: radial-gradient(circle at 30% 28%, #fff, #93c5fd 36%, var(--op-brand) 100%);
  box-shadow: 0 10px 24px rgba(7, 17, 31, 0.22);
}

.orb-a {
  width: 16px;
  height: 16px;
  left: 22%;
  bottom: 32%;
  animation: floaty 5.5s ease-in-out infinite;
}

.orb-b {
  width: 22px;
  height: 22px;
  left: 30%;
  top: 27%;
  animation: floaty 8.2s ease-in-out infinite;
}

.spark {
  position: absolute;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.85);
  box-shadow: 0 0 12px rgba(255, 255, 255, 0.7);
}

.s1 { left: 18%; top: 38%; }
.s2 { left: 48%; top: 30%; }
.s3 { left: 38%; bottom: 26%; }

@keyframes floaty {
  0%,
  100% { translate: 0 0; }
  50% { translate: 0 -10px; }
}

@keyframes orbit {
  from { transform: rotateX(66deg) rotateZ(-16deg) rotate(0deg); }
  to { transform: rotateX(66deg) rotateZ(-16deg) rotate(360deg); }
}

@media (max-width: 900px) {
  .scene {
    width: 100%;
    opacity: 0.35;
  }
}

@media (prefers-reduced-motion: reduce) {
  .ring,
  .planet,
  .glass,
  .orb {
    animation: none;
  }
}
</style>
