// The classic look's places (#70, #121): [x, z, facing, pose]. Data only, so tests read it without three.js.
export const LOUNGE_SEATS = [[11.8, 12.0], [13.5, 12.0], [15.2, 12.0], [10.4, 8.6], [12.4, 8.6], [14.6, 8.6], [16.6, 8.6], [11.4, 7.2], [15.6, 7.2]];
export const BREAK_SPOTS = [
  { name: "coffee machine", places: [[18.6, 5.8, 0, "sip"], [17.4, 7.0, Math.PI / 2, "sip"]] },
  { name: "table tennis", min: 2, places: [[-1.9, 10.2, Math.PI / 2, "play"], [1.9, 10.2, -Math.PI / 2, "play"]] },
  { name: "sofa", places: [[11.8, 12.0, Math.PI, "sit"], [13.5, 12.0, Math.PI, "sit"], [15.2, 12.0, Math.PI, "sit"], [11.4, 7.2, 0, "sit"], [15.6, 7.2, 0, "sit"]] },
  { name: "bookshelf", places: [[-2.8, 13.6, 0.7, "read"], [-6.4, 13.6, 0.5, "read"]] },
  { name: "window", places: [[-20.1, 1.8, -Math.PI / 2, "stand"], [-20.1, 3.0, -Math.PI / 2, "stand"]] },
  { name: "stroll", places: [[-17.6, 2.4, -Math.PI / 2, "stand"], [16.2, 6.4, 0, "stand"], [0, 7.6, 0, "stand"],
                             [-4.6, 12.2, 0.6, "stand"], [13.5, 10.3, 0, "stand"], [-9.0, 6.0, 0, "stand"]] },
];
export const LAYOUT = { breakSpots: BREAK_SPOTS, loungeSeats: LOUNGE_SEATS.length, meetingSeats: 8 };
