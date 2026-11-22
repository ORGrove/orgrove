// The classic look's places (#70, #121): [x, z, facing, pose]. Data only, so tests read it without three.js.
export const LOUNGE_SEATS = [[11.8, 12.0], [13.5, 12.0], [15.2, 12.0], [10.4, 8.6], [12.4, 8.6], [14.6, 8.6], [16.6, 8.6], [11.4, 7.2], [15.6, 7.2]];
export const FOOSBALL = [5.2, 10.2];                     // table centre (2026-11-03), beside the table tennis; drawn 1.6x
export const BREAK_SPOTS = [
  { name: "coffee machine", places: [[18.6, 5.8, 0, "sip"], [17.4, 7.0, Math.PI / 2, "sip"]] },
  { name: "table tennis", min: 2, places: [[-1.9, 10.2, Math.PI / 2, "play"], [1.9, 10.2, -Math.PI / 2, "play"]] },
  { name: "foosball", min: 2, places: [[FOOSBALL[0] + 0.12, FOOSBALL[1] - 1.36, 0, "foos"], [FOOSBALL[0] - 0.12, FOOSBALL[1] + 1.36, Math.PI, "foos"]] },
  { name: "sofa", places: [[11.8, 12.0, Math.PI, "sit"], [13.5, 12.0, Math.PI, "sit"], [15.2, 12.0, Math.PI, "sit"], [11.4, 7.2, 0, "sit"], [15.6, 7.2, 0, "sit"]] },
  { name: "bookshelf", places: [[-2.8, 13.6, 0.7, "read"], [-6.4, 13.6, 0.5, "read"]] },
  { name: "window", places: [[-20.1, 1.8, -Math.PI / 2, "stand"], [-20.1, 3.0, -Math.PI / 2, "stand"]] },
  { name: "stroll", places: [[-17.6, 2.4, -Math.PI / 2, "stand"], [16.2, 6.4, 0, "stand"], [0, 7.6, 0, "stand"],
                             [-4.6, 12.2, 0.6, "stand"], [13.5, 10.3, 0, "stand"], [-9.0, 6.0, 0, "stand"]] },
];
export const LAYOUT = { breakSpots: BREAK_SPOTS, loungeSeats: LOUNGE_SEATS.length, meetingSeats: 13 };   // 13 since 2026-11-02 (layout_new.js)
// "Your coding sessions" desk (plan 2026-11-20): the existing front-left desk, four seats facing the back wall (owner,
// 2026-11-21: four, so it does not look odd). No more desks: the room must never look crowded; a fifth session is a
// "+1 more" tag, in full on the 2D page.
export const SESSION_DESKS = [[-14, 9.8]];
export const SESSION_SEAT_DX = [-1.5, -0.5, 0.5, 1.5];
export const SESSION_SEATS = SESSION_DESKS.flatMap(([x, z]) => SESSION_SEAT_DX.map((dx) => [x + dx, z + 1.0, Math.PI]));
export const SESSION_DOOR = [-20.4, 4.2];                // where a session walks in from, and out to
