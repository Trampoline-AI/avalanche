const WEEKDAYS = [
  "Sunday",
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];
const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

function number(field: string, min: number, max: number): number | undefined {
  if (!/^\d+$/.test(field)) return undefined;
  const value = Number(field);
  return value >= min && value <= max ? value : undefined;
}

function step(field: string, max: number): number | undefined {
  return field.startsWith("*/") ? number(field.slice(2), 1, max) : undefined;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * Describe common five-field cron expressions in plain words, in the operator's
 * local time. Returns undefined for expressions it does not recognize.
 */
export function describeCron(expression: string): string | undefined {
  const fields = expression.trim().split(/\s+/);
  if (fields.length !== 5) return undefined;
  const [minuteField, hourField, dayField, monthField, weekdayField] = fields;
  const everyDay = dayField === "*" && monthField === "*";

  if (hourField === "*" && everyDay && weekdayField === "*") {
    if (minuteField === "*") return "Every minute";
    const minutes = step(minuteField, 59);
    if (minutes !== undefined)
      return minutes === 1 ? "Every minute" : `Every ${minutes} minutes`;
    const minute = number(minuteField, 0, 59);
    if (minute === undefined) return undefined;
    return minute === 0 ? "Every hour, on the hour" : `Every hour at :${pad(minute)}`;
  }

  const minute = number(minuteField, 0, 59);
  if (minute === undefined) return undefined;
  const hours = step(hourField, 23);
  if (hours !== undefined && everyDay && weekdayField === "*") {
    return `Every ${hours} hours at :${pad(minute)}`;
  }
  const hour = number(hourField, 0, 23);
  if (hour === undefined) return undefined;
  const time = `${pad(hour)}:${pad(minute)}`;

  if (everyDay) {
    if (weekdayField === "*") return `Every day at ${time}`;
    if (weekdayField === "1-5") return `Every weekday at ${time}`;
    const weekday = number(weekdayField, 0, 7);
    return weekday === undefined ? undefined : `Every ${WEEKDAYS[weekday]} at ${time}`;
  }
  if (weekdayField !== "*") return undefined;
  const day = number(dayField, 1, 31);
  if (day === undefined) return undefined;
  if (monthField === "*") return `Monthly on day ${day} at ${time}`;
  const month = number(monthField, 1, 12);
  return month === undefined
    ? undefined
    : `Every year on ${MONTHS[month - 1]} ${day} at ${time}`;
}
