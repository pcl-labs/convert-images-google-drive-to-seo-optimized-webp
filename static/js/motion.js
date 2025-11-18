(function () {
  const durationFallbacks = {
    short1: 50,
    short2: 100,
    short3: 150,
    short4: 200,
    medium1: 250,
    medium2: 300,
    medium3: 350,
    medium4: 400,
    long1: 450,
    long2: 500,
    long3: 550,
    long4: 600,
  };

  const easingFallbacks = {
    standard: 'cubic-bezier(0.2, 0, 0, 1)',
    'standard-accelerate': 'cubic-bezier(0.3, 0, 1, 1)',
    'standard-decelerate': 'cubic-bezier(0, 0, 0, 1)',
    emphasized: 'cubic-bezier(0.2, 0, 0, 1)',
    'emphasized-accelerate': 'cubic-bezier(0.3, 0, 0.8, 0.15)',
    'emphasized-decelerate': 'cubic-bezier(0.05, 0.7, 0.1, 1)',
  };

  const docStyle = () => window.getComputedStyle(document.documentElement);

  function readDuration(token) {
    const normalized = String(token).replace(/[_\s-]+/g, '').toLowerCase();
    const cssKey = normalized.replace(/(short|medium|long)(\d)/, '$1$2');
    const varName = `--md-sys-motion-duration-${cssKey}`;
    const raw = docStyle().getPropertyValue(varName);
    if (raw && raw.trim()) {
      return raw.trim();
    }
    const fallback = durationFallbacks[cssKey] || durationFallbacks.medium2;
    return `${fallback}ms`;
  }

  function readEasing(token) {
    const normalized = String(token)
      .replace(/([a-z])([A-Z])/g, '$1-$2')
      .replace(/[_\s]+/g, '-')
      .toLowerCase();
    const varName = `--md-sys-motion-easing-${normalized}`;
    const raw = docStyle().getPropertyValue(varName);
    if (raw && raw.trim()) {
      return raw.trim();
    }
    return easingFallbacks[normalized] || easingFallbacks.standard;
  }

  function applyTransition(element, options = {}) {
    if (!element) return;
    const {
      property = 'opacity, transform',
      duration = 'medium2',
      easing = 'standard',
      delay = 0,
    } = options;
    const durationValue = readDuration(duration);
    const easingValue = readEasing(easing);
    element.style.transitionProperty = property;
    element.style.transitionDuration = durationValue;
    element.style.transitionTimingFunction = easingValue;
    element.style.transitionDelay = typeof delay === 'number' ? `${delay}ms` : delay;
  }

  window.materialMotion = {
    duration: readDuration,
    easing: readEasing,
    applyTransition,
    tokens: {
      durations: { ...durationFallbacks },
      easings: { ...easingFallbacks },
    },
  };
})();
