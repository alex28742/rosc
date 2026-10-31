export const text = (form: FormData, name: string): string => String(form.get(name) ?? '');
