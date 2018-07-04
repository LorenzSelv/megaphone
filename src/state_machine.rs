//! General purpose state transition operator.
use std::hash::Hash;

use fnv::FnvHashMap as HashMap;

use timely::ExchangeData;
use timely::dataflow::{Stream, Scope};
use timely::dataflow::channels::pact::Pipeline;
use timely::dataflow::operators::Probe;
use timely::Data;
use timely::dataflow::operators::Operator;

use stateful::{StateHandle, StateStream};

pub trait BinnedStateMachine<S: Scope, K: ExchangeData+Hash+Eq, V: ExchangeData, D: ExchangeData + Default + 'static> {
    /// Tracks a state for each presented key, using user-supplied state transition logic.
    ///
    /// The transition logic `fold` may mutate the state, and produce both output records and
    /// a `bool` indicating that it is appropriate to deregister the state, cleaning up once
    /// the state is no longer helpful.
    ///
    /// #Examples
    /// ```
    /// ```
    fn state_machine<
        R: Data,                                    // output type
        I: IntoIterator<Item=R>,                    // type of output iterator
        F: Fn(&K, V, &mut D)->(bool, I)+'static,    // state update logic
    >(&mut self, fold: F) -> Stream<S, R> where S::Timestamp : Hash+Eq ;
}

impl<S, K, V, D> BinnedStateMachine<S, K, V, D> for StateStream<S, (K, V), HashMap<K, D>, (K, D)>
where
    S: Scope,
    K: ExchangeData+Hash+Eq,
    V: ExchangeData,
    D: ExchangeData + Default + 'static,
{
    fn state_machine<
        R: Data,                                    // output type
        I: IntoIterator<Item=R>,                    // type of output iterator
        F: Fn(&K, V, &mut D) -> (bool, I) + 'static,    // state update logic
    >(&mut self, fold: F) -> Stream<S, R> where S::Timestamp: Hash + Eq {
        // times -> Vec<input>
        let mut pending: HashMap<_, Vec<(_, _, (K, V))>> = Default::default();

        let states = self.state.clone();

        self.stream.unary_frontier(Pipeline, "StateMachine", |_cap, _info| {
            move |input, output| {

                let frontier = input.frontier();

                // go through each time with data, process each (key, val) pair.
                let mut states = states.borrow_mut();
                while let Some(time) = states.notificator().next(&[frontier]) {
                    if let Some(pend) = pending.remove(time.time()) {
                        let mut session = output.session(&time);
                        for (_target, key_id, (key, val)) in pend {
                            let mut states = states.get_state(key_id);
                            let (remove, output) = {
                                let state = states.entry(key.clone()).or_insert_with(Default::default);
                                fold(&key, val.clone(), state)
                            };
                            if remove { states.remove(&key); }
                            session.give_iterator(output.into_iter());
                        }
                    }
                }

                // stash each input and request a notification when ready
                while let Some((time, data)) = input.next() {
                    // stash if not time yet
                    if frontier.less_than(time.time()) {
                        pending.entry(time.time().clone()).or_insert_with(Vec::new).extend(data.drain(..));
                        states.notificator().notify_at(time.retain());
                    } else {
                        // else we can process immediately
                        let mut session = output.session(&time);
                        for (_target, key_id, (key, val)) in data.drain(..) {
                            let mut states = states.get_state(key_id);
                            let (remove, output) = {
                                let state = states.entry(key.clone()).or_insert_with(Default::default);
                                fold(&key, val.clone(), state)
                            };
                            if remove { states.remove(&key); }
                            session.give_iterator(output.into_iter());
                        }
                    }
                };
            }
        }).probe_with(&mut self.probe)
    }
}
