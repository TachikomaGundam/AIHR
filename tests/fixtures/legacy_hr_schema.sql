--
-- PostgreSQL database dump
--


-- Dumped from database version 16.14
-- Dumped by pg_dump version 16.14

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: hr; Type: SCHEMA; Schema: -; Owner: wikijs
--

CREATE SCHEMA hr;



SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: assignment; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.assignment (
    assignment_id text NOT NULL,
    seat_code text NOT NULL,
    primary_model text NOT NULL,
    fallback1 text,
    fallback2 text,
    fallback3 text,
    reason_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    assigned_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: battery; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.battery (
    battery_id text NOT NULL,
    battery_code text NOT NULL,
    version text DEFAULT 'v1'::text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: battery_item; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.battery_item (
    battery_id text NOT NULL,
    item_id text NOT NULL,
    weight numeric(6,3) DEFAULT 1.0 NOT NULL,
    "position" integer DEFAULT 0 NOT NULL
);



--
-- Name: calibration_event; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.calibration_event (
    event_id text NOT NULL,
    item_id text NOT NULL,
    kind text NOT NULL,
    before_se numeric(10,6),
    after_se numeric(10,6),
    evidence_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: control_model; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.control_model (
    provider_fk text NOT NULL,
    model_id text NOT NULL,
    mode text DEFAULT 'primary'::text NOT NULL
);



--
-- Name: control_reading; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.control_reading (
    reading_id text NOT NULL,
    control_model_fk text NOT NULL,
    battery_id text NOT NULL,
    round integer NOT NULL,
    read_at timestamp with time zone DEFAULT now() NOT NULL,
    result_json jsonb DEFAULT '{}'::jsonb NOT NULL
);



--
-- Name: infra_incident; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.infra_incident (
    incident_id text NOT NULL,
    run_id text NOT NULL,
    kind text NOT NULL,
    details_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: item; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.item (
    item_id text NOT NULL,
    model_id text NOT NULL,
    payload_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    difficulty_class text DEFAULT 'mid'::text NOT NULL,
    calibrated_se numeric(10,6),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: item_pool; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.item_pool (
    item_id text NOT NULL,
    item_code text NOT NULL,
    version text DEFAULT 'v1'::text NOT NULL,
    domain text NOT NULL,
    kind text NOT NULL,
    json_meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: judge_verdict; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.judge_verdict (
    verdict_id text NOT NULL,
    sweep_id text NOT NULL,
    item_id text NOT NULL,
    model_id text NOT NULL,
    round integer DEFAULT 1 NOT NULL,
    judgement_json jsonb DEFAULT '{}'::jsonb NOT NULL
);



--
-- Name: measurement; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.measurement (
    measurement_id text NOT NULL,
    run_id text NOT NULL,
    item_id text NOT NULL,
    repetition integer DEFAULT 1 NOT NULL,
    score numeric(10,6) NOT NULL,
    tokens_in integer,
    tokens_out integer,
    latency_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    response_text text,
    thinking_text text,
    requested_max_output integer
);



--
-- Name: model; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.model (
    model_id text NOT NULL,
    provider_fk text NOT NULL,
    model_name text NOT NULL,
    capabilities jsonb DEFAULT '{}'::jsonb NOT NULL,
    default_rpm integer,
    default_cost_1k_in numeric(12,6),
    default_cost_1k_out numeric(12,6),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: policy_override; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.policy_override (
    override_id text NOT NULL,
    seat_code text NOT NULL,
    rule text NOT NULL,
    before_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    after_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    reason text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: provider; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.provider (
    provider_id text NOT NULL,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: run; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.run (
    run_id text NOT NULL,
    sweep_id text NOT NULL,
    model_id text NOT NULL,
    battery_id text NOT NULL,
    round integer DEFAULT 1 NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    total_tokens integer,
    total_cost_cny numeric(12,4),
    infra_ok boolean DEFAULT true NOT NULL
);



--
-- Name: seat; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.seat (
    seat_code text NOT NULL,
    seat_name text NOT NULL,
    domain text NOT NULL,
    domain_specificity numeric(4,3) DEFAULT 0.5 NOT NULL,
    cost_tier text DEFAULT 'mid'::text NOT NULL,
    budget_tier text DEFAULT 'mid'::text NOT NULL,
    required_capabilities jsonb DEFAULT '[]'::jsonb NOT NULL,
    ctx_p95_tokens integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: seat_battery; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.seat_battery (
    seat_code text NOT NULL,
    battery_id text NOT NULL,
    n_initial integer DEFAULT 3 NOT NULL,
    n_max integer DEFAULT 10 NOT NULL
);



--
-- Name: separation; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.separation (
    separation_id text NOT NULL,
    sweep_id text NOT NULL,
    battery_id text NOT NULL,
    model_a text NOT NULL,
    model_b text NOT NULL,
    p_separated numeric(6,4) NOT NULL,
    p_weak numeric(6,4) NOT NULL,
    p_tie numeric(6,4) NOT NULL,
    estimated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT separation_check CHECK ((model_a <> model_b))
);



--
-- Name: sweep; Type: TABLE; Schema: hr; Owner: wikijs
--

CREATE TABLE hr.sweep (
    sweep_id text NOT NULL,
    seat_code text NOT NULL,
    purpose text DEFAULT 'primary'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);



--
-- Name: assignment assignment_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.assignment
    ADD CONSTRAINT assignment_pkey PRIMARY KEY (assignment_id);


--
-- Name: battery battery_battery_code_key; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.battery
    ADD CONSTRAINT battery_battery_code_key UNIQUE (battery_code);


--
-- Name: battery_item battery_item_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.battery_item
    ADD CONSTRAINT battery_item_pkey PRIMARY KEY (battery_id, item_id);


--
-- Name: battery battery_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.battery
    ADD CONSTRAINT battery_pkey PRIMARY KEY (battery_id);


--
-- Name: calibration_event calibration_event_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.calibration_event
    ADD CONSTRAINT calibration_event_pkey PRIMARY KEY (event_id);


--
-- Name: control_model control_model_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.control_model
    ADD CONSTRAINT control_model_pkey PRIMARY KEY (provider_fk);


--
-- Name: control_reading control_reading_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.control_reading
    ADD CONSTRAINT control_reading_pkey PRIMARY KEY (reading_id);


--
-- Name: infra_incident infra_incident_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.infra_incident
    ADD CONSTRAINT infra_incident_pkey PRIMARY KEY (incident_id);


--
-- Name: item item_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.item
    ADD CONSTRAINT item_pkey PRIMARY KEY (item_id);


--
-- Name: item_pool item_pool_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.item_pool
    ADD CONSTRAINT item_pool_pkey PRIMARY KEY (item_id);


--
-- Name: judge_verdict judge_verdict_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.judge_verdict
    ADD CONSTRAINT judge_verdict_pkey PRIMARY KEY (verdict_id);


--
-- Name: judge_verdict judge_verdict_sweep_id_item_id_model_id_round_key; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.judge_verdict
    ADD CONSTRAINT judge_verdict_sweep_id_item_id_model_id_round_key UNIQUE (sweep_id, item_id, model_id, round);


--
-- Name: measurement measurement_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.measurement
    ADD CONSTRAINT measurement_pkey PRIMARY KEY (measurement_id);


--
-- Name: measurement measurement_run_id_item_id_repetition_key; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.measurement
    ADD CONSTRAINT measurement_run_id_item_id_repetition_key UNIQUE (run_id, item_id, repetition);


--
-- Name: model model_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.model
    ADD CONSTRAINT model_pkey PRIMARY KEY (model_id);


--
-- Name: policy_override policy_override_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.policy_override
    ADD CONSTRAINT policy_override_pkey PRIMARY KEY (override_id);


--
-- Name: provider provider_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.provider
    ADD CONSTRAINT provider_pkey PRIMARY KEY (provider_id);


--
-- Name: run run_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.run
    ADD CONSTRAINT run_pkey PRIMARY KEY (run_id);


--
-- Name: seat_battery seat_battery_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.seat_battery
    ADD CONSTRAINT seat_battery_pkey PRIMARY KEY (seat_code, battery_id);


--
-- Name: seat seat_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.seat
    ADD CONSTRAINT seat_pkey PRIMARY KEY (seat_code);


--
-- Name: separation separation_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.separation
    ADD CONSTRAINT separation_pkey PRIMARY KEY (separation_id);


--
-- Name: sweep sweep_pkey; Type: CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.sweep
    ADD CONSTRAINT sweep_pkey PRIMARY KEY (sweep_id);


--
-- Name: assignment_seat_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX assignment_seat_idx ON hr.assignment USING btree (seat_code);


--
-- Name: calibration_item_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX calibration_item_idx ON hr.calibration_event USING btree (item_id);


--
-- Name: incident_run_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX incident_run_idx ON hr.infra_incident USING btree (run_id);


--
-- Name: item_model_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX item_model_idx ON hr.item USING btree (model_id);


--
-- Name: item_pool_domain_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX item_pool_domain_idx ON hr.item_pool USING btree (domain);


--
-- Name: judge_sweep_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX judge_sweep_idx ON hr.judge_verdict USING btree (sweep_id);


--
-- Name: measurement_item_score; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX measurement_item_score ON hr.measurement USING btree (item_id, score);


--
-- Name: measurement_run_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX measurement_run_idx ON hr.measurement USING btree (run_id);


--
-- Name: model_provider_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX model_provider_idx ON hr.model USING btree (provider_fk);


--
-- Name: run_model_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX run_model_idx ON hr.run USING btree (model_id);


--
-- Name: run_sweep_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX run_sweep_idx ON hr.run USING btree (sweep_id);


--
-- Name: separation_sweep_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX separation_sweep_idx ON hr.separation USING btree (sweep_id);


--
-- Name: sweep_seat_idx; Type: INDEX; Schema: hr; Owner: wikijs
--

CREATE INDEX sweep_seat_idx ON hr.sweep USING btree (seat_code);


--
-- Name: assignment assignment_fallback1_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.assignment
    ADD CONSTRAINT assignment_fallback1_fkey FOREIGN KEY (fallback1) REFERENCES hr.model(model_id);


--
-- Name: assignment assignment_fallback2_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.assignment
    ADD CONSTRAINT assignment_fallback2_fkey FOREIGN KEY (fallback2) REFERENCES hr.model(model_id);


--
-- Name: assignment assignment_fallback3_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.assignment
    ADD CONSTRAINT assignment_fallback3_fkey FOREIGN KEY (fallback3) REFERENCES hr.model(model_id);


--
-- Name: assignment assignment_primary_model_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.assignment
    ADD CONSTRAINT assignment_primary_model_fkey FOREIGN KEY (primary_model) REFERENCES hr.model(model_id);


--
-- Name: assignment assignment_seat_code_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.assignment
    ADD CONSTRAINT assignment_seat_code_fkey FOREIGN KEY (seat_code) REFERENCES hr.seat(seat_code);


--
-- Name: battery_item battery_item_battery_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.battery_item
    ADD CONSTRAINT battery_item_battery_id_fkey FOREIGN KEY (battery_id) REFERENCES hr.battery(battery_id);


--
-- Name: battery_item battery_item_item_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.battery_item
    ADD CONSTRAINT battery_item_item_id_fkey FOREIGN KEY (item_id) REFERENCES hr.item_pool(item_id);


--
-- Name: calibration_event calibration_event_item_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.calibration_event
    ADD CONSTRAINT calibration_event_item_id_fkey FOREIGN KEY (item_id) REFERENCES hr.item_pool(item_id);


--
-- Name: control_model control_model_model_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.control_model
    ADD CONSTRAINT control_model_model_id_fkey FOREIGN KEY (model_id) REFERENCES hr.model(model_id);


--
-- Name: control_model control_model_provider_fk_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.control_model
    ADD CONSTRAINT control_model_provider_fk_fkey FOREIGN KEY (provider_fk) REFERENCES hr.provider(provider_id);


--
-- Name: control_reading control_reading_battery_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.control_reading
    ADD CONSTRAINT control_reading_battery_id_fkey FOREIGN KEY (battery_id) REFERENCES hr.battery(battery_id);


--
-- Name: control_reading control_reading_control_model_fk_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.control_reading
    ADD CONSTRAINT control_reading_control_model_fk_fkey FOREIGN KEY (control_model_fk) REFERENCES hr.model(model_id);


--
-- Name: infra_incident infra_incident_run_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.infra_incident
    ADD CONSTRAINT infra_incident_run_id_fkey FOREIGN KEY (run_id) REFERENCES hr.run(run_id);


--
-- Name: item item_item_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.item
    ADD CONSTRAINT item_item_id_fkey FOREIGN KEY (item_id) REFERENCES hr.item_pool(item_id);


--
-- Name: item item_model_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.item
    ADD CONSTRAINT item_model_id_fkey FOREIGN KEY (model_id) REFERENCES hr.model(model_id);


--
-- Name: judge_verdict judge_verdict_item_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.judge_verdict
    ADD CONSTRAINT judge_verdict_item_id_fkey FOREIGN KEY (item_id) REFERENCES hr.item_pool(item_id);


--
-- Name: judge_verdict judge_verdict_model_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.judge_verdict
    ADD CONSTRAINT judge_verdict_model_id_fkey FOREIGN KEY (model_id) REFERENCES hr.model(model_id);


--
-- Name: judge_verdict judge_verdict_sweep_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.judge_verdict
    ADD CONSTRAINT judge_verdict_sweep_id_fkey FOREIGN KEY (sweep_id) REFERENCES hr.sweep(sweep_id);


--
-- Name: measurement measurement_item_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.measurement
    ADD CONSTRAINT measurement_item_id_fkey FOREIGN KEY (item_id) REFERENCES hr.item_pool(item_id);


--
-- Name: measurement measurement_run_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.measurement
    ADD CONSTRAINT measurement_run_id_fkey FOREIGN KEY (run_id) REFERENCES hr.run(run_id);


--
-- Name: model model_provider_fk_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.model
    ADD CONSTRAINT model_provider_fk_fkey FOREIGN KEY (provider_fk) REFERENCES hr.provider(provider_id);


--
-- Name: policy_override policy_override_seat_code_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.policy_override
    ADD CONSTRAINT policy_override_seat_code_fkey FOREIGN KEY (seat_code) REFERENCES hr.seat(seat_code);


--
-- Name: run run_battery_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.run
    ADD CONSTRAINT run_battery_id_fkey FOREIGN KEY (battery_id) REFERENCES hr.battery(battery_id);


--
-- Name: run run_model_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.run
    ADD CONSTRAINT run_model_id_fkey FOREIGN KEY (model_id) REFERENCES hr.model(model_id);


--
-- Name: run run_sweep_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.run
    ADD CONSTRAINT run_sweep_id_fkey FOREIGN KEY (sweep_id) REFERENCES hr.sweep(sweep_id);


--
-- Name: seat_battery seat_battery_battery_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.seat_battery
    ADD CONSTRAINT seat_battery_battery_id_fkey FOREIGN KEY (battery_id) REFERENCES hr.battery(battery_id);


--
-- Name: seat_battery seat_battery_seat_code_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.seat_battery
    ADD CONSTRAINT seat_battery_seat_code_fkey FOREIGN KEY (seat_code) REFERENCES hr.seat(seat_code);


--
-- Name: separation separation_battery_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.separation
    ADD CONSTRAINT separation_battery_id_fkey FOREIGN KEY (battery_id) REFERENCES hr.battery(battery_id);


--
-- Name: separation separation_model_a_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.separation
    ADD CONSTRAINT separation_model_a_fkey FOREIGN KEY (model_a) REFERENCES hr.model(model_id);


--
-- Name: separation separation_model_b_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.separation
    ADD CONSTRAINT separation_model_b_fkey FOREIGN KEY (model_b) REFERENCES hr.model(model_id);


--
-- Name: separation separation_sweep_id_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.separation
    ADD CONSTRAINT separation_sweep_id_fkey FOREIGN KEY (sweep_id) REFERENCES hr.sweep(sweep_id);


--
-- Name: sweep sweep_seat_code_fkey; Type: FK CONSTRAINT; Schema: hr; Owner: wikijs
--

ALTER TABLE ONLY hr.sweep
    ADD CONSTRAINT sweep_seat_code_fkey FOREIGN KEY (seat_code) REFERENCES hr.seat(seat_code);


--
