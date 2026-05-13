/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "../src/dashboard/templates/**/*.html",
    "../src/dashboard/static/app.js",
  ],
  plugins: [require("daisyui")],
  daisyui: {
    // Include both base themes; wasp-dark/wasp-light extend them via CSS variable overrides in style.css
    themes: ["dark", "light"],
    logs: false,
  },
}
