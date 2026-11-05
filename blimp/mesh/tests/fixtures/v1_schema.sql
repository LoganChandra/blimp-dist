CREATE TABLE public.a2a_messages (
    id bigint NOT NULL,
    task_id text NOT NULL,
    parent_task_id text,
    from_agent character varying(50) NOT NULL,
    to_agent character varying(50) NOT NULL,
    state character varying(20) DEFAULT 'submitted'::character varying NOT NULL,
    skill character varying(100),
    body jsonb NOT NULL,
    result jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    acked_at timestamp with time zone,
    bounce_count integer DEFAULT 0 NOT NULL,
    fail_reason text,
    working_at timestamp with time zone,
    input_required_at timestamp with time zone,
    completed_at timestamp with time zone,
    failed_at timestamp with time zone,
    attempt_count integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    last_error text,
    notified_at timestamp with time zone,
    delivered_at timestamp with time zone,
    escalated_at timestamp with time zone,
    dlq_reason text,
    lease_until timestamp with time zone
);
CREATE SEQUENCE public.a2a_messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.a2a_messages_id_seq OWNED BY public.a2a_messages.id;
CREATE TABLE public.tasks (
    id bigint NOT NULL,
    title text NOT NULL,
    description text,
    status character varying(20) DEFAULT 'pending'::character varying,
    priority character varying(10) DEFAULT 'P2'::character varying,
    assigned_to character varying(50),
    parent_id bigint,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone
);
CREATE SEQUENCE public.tasks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.tasks_id_seq OWNED BY public.tasks.id;
ALTER TABLE ONLY public.a2a_messages ALTER COLUMN id SET DEFAULT nextval('public.a2a_messages_id_seq'::regclass);
ALTER TABLE ONLY public.tasks ALTER COLUMN id SET DEFAULT nextval('public.tasks_id_seq'::regclass);
ALTER TABLE ONLY public.a2a_messages
    ADD CONSTRAINT a2a_messages_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);
CREATE INDEX a2a_parent_idx ON public.a2a_messages USING btree (parent_task_id);
CREATE INDEX a2a_task_idx ON public.a2a_messages USING btree (task_id);
CREATE INDEX a2a_to_state_idx ON public.a2a_messages USING btree (to_agent, state);
CREATE INDEX tasks_assigned_idx ON public.tasks USING btree (assigned_to, status);
CREATE INDEX tasks_status_idx ON public.tasks USING btree (status);
ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.tasks(id);
