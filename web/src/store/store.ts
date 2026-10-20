import { configureStore, createSlice, type PayloadAction } from "@reduxjs/toolkit";

import type { PreviewResultado } from "../api/client";

interface StudioState {
  token: string | null;
  pipelineId: string | null;
  baseNumero: number;
  preview: PreviewResultado | null;
  erro: string | null;
}

// Token rehidratado do localStorage → refresh não derruba a sessão.
const TOKEN_KEY = "cflow_token";
const tokenInicial = typeof localStorage !== "undefined" ? localStorage.getItem(TOKEN_KEY) : null;

const inicial: StudioState = { token: tokenInicial, pipelineId: null, baseNumero: 0, preview: null, erro: null };

const studio = createSlice({
  name: "studio",
  initialState: inicial,
  reducers: {
    setToken: (s, a: PayloadAction<string | null>) => {
      s.token = a.payload;
      try {
        if (a.payload) localStorage.setItem(TOKEN_KEY, a.payload);
        else localStorage.removeItem(TOKEN_KEY);
      } catch { /* localStorage indisponível */ }
    },
    setPipeline: (s, a: PayloadAction<{ id: string; numero: number }>) => {
      s.pipelineId = a.payload.id; s.baseNumero = a.payload.numero;
    },
    setBaseNumero: (s, a: PayloadAction<number>) => { s.baseNumero = a.payload; },
    setPreview: (s, a: PayloadAction<PreviewResultado | null>) => { s.preview = a.payload; s.erro = null; },
    setErro: (s, a: PayloadAction<string | null>) => { s.erro = a.payload; s.preview = null; },
  },
});

export const { setToken, setPipeline, setBaseNumero, setPreview, setErro } = studio.actions;

export const store = configureStore({ reducer: { studio: studio.reducer } });
export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
