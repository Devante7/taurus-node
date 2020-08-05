#pragma once
#include <appbase/application.hpp>
#include <eosio/witness_plugin/wrapped_weak_shared_ptr.hpp>
#include <eosio/signature_provider_plugin/signature_provider_plugin.hpp>
#include <eosio/chain_plugin/chain_plugin.hpp>

namespace eosio {

using namespace appbase;
using namespace witness_plugin_wrappers;

class witness_plugin : public appbase::plugin<witness_plugin> {
public:
   witness_plugin();

   using witness_callback_func = std::function<void(const chain::block_state_ptr& bsp, const chain::signature_type& sig)>;

   APPBASE_PLUGIN_REQUIRES((signature_provider_plugin)(chain_plugin))
   virtual void set_program_options(options_description& cli, options_description& cfg) override;

   void plugin_initialize(const variables_map& options);
   void plugin_startup();
   void plugin_shutdown();

   /*
    * Add a callback for when a witness signature is created. This function may only be called from a plugin's
    * initialize() or startup() (and on the main thread, of course). The callback will be on a non-main thread.
    * Similar to how boost signals2 object tracking works, a weak_ptr is required which is "locked" once the witness
    * plugin begins to create a signature. This will allow any dependent plugin's impl to stay alive until the callback
    * is fired -- even if appbase has already initiated shutdown and called shutdown() of the dependent plugin.
    */
   template<typename T>
   void add_on_witness_sig(witness_callback_func&& func, std::weak_ptr<T> weak_ptr) {
      auto p = std::make_unique<wrapped_weak_ptr<T>>(weak_ptr);
      add_on_witness_sig(std::move(func), std::move(p));
   }

private:
   void add_on_witness_sig(witness_callback_func&& func, std::unique_ptr<wrapped_weak_ptr_base>&& weak_ptr);

   std::unique_ptr<struct witness_plugin_impl> my;
};

}
