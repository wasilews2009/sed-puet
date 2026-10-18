/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "media",
  theme: {
    extend: {
      colors: {
        // фірмові відтінки ПУЕТ (бордовий) + нейтральні; контраст тексту ≥ 4.5:1
        brand: { 50: "#fbf3f4", 100: "#f5e1e4", 600: "#9b1b30", 700: "#7f1527", 800: "#651020" },
      },
      fontFamily: { sans: ["Inter", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif"] },
    },
  },
  plugins: [],
};
