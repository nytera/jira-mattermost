/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Surfaces — deep ink base, raised panels.
        ink: "#0E1116",
        panel: "#161B22",
        raised: "#1C2230",
        hover: "#222B38",
        line: "rgba(230,237,243,0.08)",
        line2: "rgba(230,237,243,0.14)",
        // Text.
        fg: "#E6EDF3",
        muted: "#8B949E",
        faint: "#5A6472",
        // Semantics — color encodes validity / lifecycle, never decoration.
        valid: "#3FB950",
        falsealarm: "#D29922",
        expected: "#A371F7",
        danger: "#F85149",
        // The single bright "live" accent — only active / open states.
        live: "#2F81F7",
      },
      fontFamily: {
        sans: ['"Hanken Grotesk"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      fontSize: {
        "readout": ["2.75rem", { lineHeight: "1", letterSpacing: "-0.02em" }],
      },
      boxShadow: {
        panel: "0 1px 0 0 rgba(230,237,243,0.04) inset, 0 8px 24px -12px rgba(0,0,0,0.6)",
        glow: "0 0 0 1px rgba(47,129,247,0.4), 0 0 24px -4px rgba(47,129,247,0.45)",
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-live": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.45" },
        },
        "sweep": {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.5s cubic-bezier(0.22,1,0.36,1) both",
        "pulse-live": "pulse-live 2s ease-in-out infinite",
        sweep: "sweep 2.2s linear infinite",
      },
    },
  },
  plugins: [],
};
