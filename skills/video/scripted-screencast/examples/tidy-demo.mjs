// Example demo for the Tidy sample app. Render it with:
//   node ../scripts/render.mjs tidy-demo.mjs --preview
//   node ../scripts/render.mjs tidy-demo.mjs --vertical
const tasks = [
  ["Review homepage copy", "High", "9:00", true],
  ["Send the venue deposit", "High", "9:30", true],
  ["Reply to the print shop", "Med", "10:00", false],
  ["Draft the launch email", "High", "11:00", false],
  ["Book a table for Friday", "Low", "Noon", false],
  ["Pick photos for the gallery", "Med", "1:00", false],
  ["Call the framer about sizes", "Low", "2:30", false],
  ["Order more tote bags", "Med", "3:00", false],
  ["Update the price list", "Med", "4:00", false],
  ["Water the plants", "Low", "5:00", false],
  ["Pay the studio rent", "High", "6:00", false],
  ["Plan Saturday's workshop", "Med", "7:00", false],
].map(([title, tag, due, done]) => ({ title, tag, due, done }));

export default {
  url: "tidy/index.html",
  viewport: { width: 1280, height: 720 },
  look: { background: "peach", title: "Tidy" },

  // Staged before recording starts: a believable day, already in progress.
  setup: [
    { localStorage: { "tidy.tasks": tasks } },
    { reload: true },
  ],

  steps: [
    { caption: "Add a task in seconds" },
    { click: "#newBtn" },
    { type: "Sign the gallery contract", into: "#title" },
    { click: { role: "button", name: "High" } },
    { click: "#addBtn", hold: 1200 },
    { caption: "Tick things off as you go" },
    { click: { role: "button", name: "Complete Reply to the print shop" } },
    { click: { role: "button", name: "Complete Draft the launch email" }, hold: 900 },
    { caption: "Everything else is one scroll away" },
    { move: [0.6, 0.6] },
    { scroll: 400 },
    { click: { role: "button", name: "Complete Plan Saturday's workshop" }, hold: 900 },
    { caption: "" },
    { zoom: "off" },
    { scroll: -400, hold: 800 },
  ],
};
